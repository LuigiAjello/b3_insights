"""
validar_pipeline.py
===================
Verifica se o pipeline completo está funcional na EC2.
Exit code 0 = tudo OK, 1 = algum check falhou.

Uso: python scripts/validar_pipeline.py
"""

import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import requests

from scraper.config import (
    S3_PREFIX_BRONZE_FATOS, S3_PREFIX_BRONZE_PRECOS,
    S3_PREFIX_SILVER_FATOS, S3_PREFIX_SILVER_PRECOS,
    S3_PREFIX_GOLD_FATOS_PRECOS, S3_PREFIX_GOLD_RESUMO,
    EMPRESAS,
)
from storage.s3_manager import listar, ler_parquet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

FLASK_PORT = os.environ.get("FLASK_PORT", "5050")

checks_ok = []
checks_fail = []


def check(nome: str, ok: bool, detalhe: str = ""):
    if ok:
        checks_ok.append(nome)
        log.info(f"  ✓ {nome}" + (f" — {detalhe}" if detalhe else ""))
    else:
        checks_fail.append(nome)
        log.error(f"  ✗ {nome}" + (f" — {detalhe}" if detalhe else ""))


def verificar_s3():
    log.info("=== [1] Verificando camadas S3 ===")
    for prefixo, nome in [
        (S3_PREFIX_BRONZE_FATOS,       "Bronze/fatos"),
        (S3_PREFIX_BRONZE_PRECOS,      "Bronze/precos"),
        (S3_PREFIX_SILVER_FATOS,       "Silver/fatos"),
        (S3_PREFIX_SILVER_PRECOS,      "Silver/precos"),
        (S3_PREFIX_GOLD_FATOS_PRECOS,  "Gold/fatos_precos"),
        (S3_PREFIX_GOLD_RESUMO,        "Gold/resumo"),
    ]:
        keys = listar(prefixo)
        parquets = [k for k in keys if k.endswith(".parquet")]
        check(nome, len(parquets) > 0, f"{len(parquets)} parquet(s)")


def verificar_silver_precos():
    log.info("=== [2] Lendo Silver de preços (primeiro ticker) ===")
    ticker = EMPRESAS[0]["ticker"]
    from scraper.config import S3_PREFIX_SILVER_PRECOS
    df = ler_parquet(f"{S3_PREFIX_SILVER_PRECOS}/{ticker}.parquet")
    check(f"Silver precos {ticker}", not df.empty, f"{len(df)} linhas, colunas: {list(df.columns)}")
    if not df.empty:
        log.info(f"\n{df.tail(3).to_string()}\n")


def verificar_gold_resumo():
    log.info("=== [3] Lendo Gold resumo ===")
    from scraper.config import S3_PREFIX_GOLD_RESUMO
    df = ler_parquet(f"{S3_PREFIX_GOLD_RESUMO}/resumo.parquet")
    check("Gold resumo_empresa", not df.empty, f"{len(df)} empresas")
    if not df.empty:
        log.info(f"\n{df[['ticker','ultimo_fechamento','variacao_dia_pct','fatos_7d']].to_string()}\n")


def verificar_api():
    log.info("=== [4] Verificando API Flask ===")
    url = f"http://localhost:{FLASK_PORT}/api/status"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        check("API /api/status", True, str(data))
    except Exception as e:
        check("API /api/status", False, str(e))


if __name__ == "__main__":
    verificar_s3()
    verificar_silver_precos()
    verificar_gold_resumo()
    verificar_api()

    log.info(f"\n{'='*50}")
    log.info(f"  Resultado: {len(checks_ok)} OK, {len(checks_fail)} FALHOU")
    if checks_fail:
        log.error(f"  Falhas: {', '.join(checks_fail)}")
    log.info(f"{'='*50}\n")

    sys.exit(0 if not checks_fail else 1)
