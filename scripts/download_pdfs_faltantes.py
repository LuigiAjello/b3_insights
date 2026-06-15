"""
download_pdfs_faltantes.py
==========================
Varre o CSV de fatos relevantes e baixa / faz upload para o S3 todos os PDFs
que ainda não estão no bucket.

Uso:
    python scripts/download_pdfs_faltantes.py
    python scripts/download_pdfs_faltantes.py --csv dados/fatos/relatorios_2025_2025.csv
"""

import sys
import logging
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import pandas as pd

from scraper.config import PASTA_PDFS, S3_PREFIX_BRONZE_PDFS, PASTA_FATOS
from scraper.scraper import normalizar_empresa, nome_arquivo_pdf, baixar_pdf
from storage.s3_manager import listar, upload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("DOWNLOAD_FALTANTES")


def s3_key_para_linha(row) -> str:
    empresa_orig = str(row.get("empresa_original", ""))
    data = str(row.get("data_entrega", ""))
    categoria = str(row.get("categoria", ""))

    subpasta = normalizar_empresa(empresa_orig)
    nome = nome_arquivo_pdf(empresa_orig, data, categoria)
    return f"{S3_PREFIX_BRONZE_PDFS}/{subpasta}/{nome}"


def chaves_existentes_no_s3(prefixo: str) -> set:
    """Retorna o conjunto de todas as keys sob o prefixo dado."""
    existentes = set(listar(prefixo))
    log.info(f"{len(existentes)} objetos encontrados no S3 sob '{prefixo}'")
    return existentes


def main(csv_path: str | None = None):
    # ── Localiza o CSV mais recente se não for fornecido ──────────────────────
    if csv_path is None:
        csvs = sorted(Path(PASTA_FATOS).glob("relatorios_*.csv"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if not csvs:
            log.error(f"Nenhum CSV encontrado em {PASTA_FATOS}. Rode o scraper primeiro.")
            sys.exit(1)
        csv_path = str(csvs[0])
    log.info(f"Lendo CSV: {csv_path}")

    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    log.info(f"{len(df)} linhas carregadas.")

    if "empresa_original" not in df.columns:
        log.error("Coluna 'empresa_original' não encontrada no CSV.")
        sys.exit(1)

    # ── Pré-carrega keys existentes no S3 ─────────────────────────────────────
    existentes = chaves_existentes_no_s3(S3_PREFIX_BRONZE_PDFS)

    pdfs_ok = 0
    pdfs_pulados = 0
    pdfs_erro = 0

    for _, row in df.iterrows():
        url = row.get("link_download") or row.get("link_documento")
        if not url:
            continue

        s3_key = s3_key_para_linha(row)

        if s3_key in existentes:
            pdfs_pulados += 1
            continue

        empresa_orig = str(row.get("empresa_original", "GERAL"))
        subpasta = normalizar_empresa(empresa_orig)
        pasta_local = Path(PASTA_PDFS) / subpasta
        pasta_local.mkdir(parents=True, exist_ok=True)

        nome_arquivo = Path(s3_key).name
        caminho_local = str(pasta_local / nome_arquivo)

        if baixar_pdf(url, caminho_local):
            if upload(caminho_local, s3_key):
                pdfs_ok += 1
                log.info(f"[OK] {s3_key}")
            else:
                log.warning(f"[ERRO upload] {s3_key}")
                pdfs_erro += 1
        else:
            log.warning(f"[ERRO download] {url}")
            pdfs_erro += 1

    print(f"\n{'='*50}")
    print(f"  CONCLUÍDO")
    print(f"  PDFs já existentes (pulados) : {pdfs_pulados}")
    print(f"  PDFs baixados e enviados     : {pdfs_ok}")
    print(f"  Erros                        : {pdfs_erro}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Baixa PDFs faltantes e faz upload para o S3")
    parser.add_argument("--csv", default=None, help="Caminho para o CSV de fatos (opcional)")
    args = parser.parse_args()
    main(csv_path=args.csv)
