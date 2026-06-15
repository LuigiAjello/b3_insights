"""
scraper.py
==========
Coleta Fatos Relevantes da B3 por trimestre via API REST.

A B3 expõe um endpoint REST que aceita um JSON base64 com o período e
retorna resultados paginados. O scraper quebra o período total em janelas
mensais (máximo 30 dias por chamada) e coleta tudo automaticamente.

Uso:
    python3 scraper/scraper.py               # interface interativa
"""

import re
import sys
import time
import base64
import json
import calendar
import argparse
import logging
import requests
import pandas as pd
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scraper.config import (
    B3_API_URL, PASTA_FATOS, PASTA_PDFS,
    S3_PREFIX_BRONZE_FATOS, S3_PREFIX_BRONZE_PDFS,
    SLEEP_ENTRE_PAGINAS, MAX_TENTATIVAS, CVM_CODES_ALVO
)
from storage.s3_manager import upload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def garantir_pastas():
    Path(PASTA_FATOS).mkdir(parents=True, exist_ok=True)
    Path(PASTA_PDFS).mkdir(parents=True, exist_ok=True)


def normalizar_empresa(nome: str) -> str:
    """Remove acentos, sufixos jurídicos e caracteres especiais."""
    import unicodedata
    nome = unicodedata.normalize('NFKD', nome)
    nome = ''.join(c for c in nome if not unicodedata.combining(c))
    nome = nome.upper()
    for s in [' S.A.', ' S/A', ' SA ', ' LTDA', ' LTDA.', ' S.A', ' - ', ' / ']:
        nome = nome.replace(s, ' ')
    nome = re.sub(r'[^A-Z0-9\s]', '', nome)
    return '_'.join(nome.split())[:40]


def nome_arquivo_pdf(empresa: str, data: str, categoria: str) -> str:
    emp = normalizar_empresa(empresa)
    dat = re.sub(r'[/ :]', '-', data)[:10]
    cat = re.sub(r'[^a-zA-Z0-9\s]', '', categoria)
    cat = '_'.join(cat.split()[:4])
    return f"{emp}_{dat}_{cat}.pdf"


def baixar_pdf(url: str, caminho: str) -> bool:
    if not url:
        return False
    if Path(caminho).exists():
        return True
    try:
        resp = requests.get(url, timeout=30, headers=HEADERS)
        resp.raise_for_status()
        with open(caminho, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        log.warning(f"  Erro PDF: {e}")
        return False


# ── API B3 ────────────────────────────────────────────────────────────────────

def _b64_params(data_ini: str, data_fim: str, pagina: int, tamanho: int = 100) -> str:
    """Codifica os parâmetros da API em base64 (formato exigido pela B3)."""
    params = {
        "language": "pt-br",
        "dateInitial": data_ini,
        "dateFinal": data_fim,
        "pageNumber": pagina,
        "pageSize": tamanho,
    }
    return base64.b64encode(json.dumps(params, separators=(',', ':')).encode()).decode()


def coletar_janela(data_inicio_iso: str, data_fim_iso: str, label: str,
                   filtrar_empresas: bool = False) -> list[dict]:
    """
    Coleta todos os registros de uma janela de datas via API REST da B3.

    Parâmetros em formato ISO (YYYY-MM-DD), paginação automática.
    Se filtrar_empresas=True, retorna apenas as 20 empresas do config.
    """
    registros = []
    pagina = 1

    while True:
        b64 = _b64_params(data_inicio_iso, data_fim_iso, pagina)
        url = f"{B3_API_URL}/{b64}"

        data = None
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            try:
                resp = requests.get(url, headers=HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                log.warning(f"  {label} pag.{pagina} tentativa {tentativa}/{MAX_TENTATIVAS} - {e}")
                if tentativa < MAX_TENTATIVAS:
                    time.sleep(5 * tentativa)
        if data is None:
            log.warning(f"  {label} pag.{pagina} - desistindo apos {MAX_TENTATIVAS} tentativas")
            break

        resultados = data.get("results", [])
        page_info  = data.get("page", {})
        total_pags = page_info.get("totalPages", 1)

        if not resultados:
            if pagina == 1:
                log.info(f"  {label} - sem resultados")
            break

        novos = []
        for r in resultados:
            company = r.get("company", {})
            cvm = company.get("codeCVM", "").strip().lstrip("0")
            if filtrar_empresas and cvm not in CVM_CODES_ALVO:
                continue
            novos.append({
                "empresa":          company.get("companyName", "").strip(),
                "ticker":           company.get("tradingName", "").strip(),
                "codigo_cvm":       company.get("codeCVM", "").strip(),
                "categoria":        r.get("category", "").strip(),
                "assunto":          r.get("subject", "").strip(),
                "tipo":             r.get("type", "").strip(),
                "especie":          r.get("kind", "").strip(),
                "status":           r.get("status", "").strip(),
                "data_referencia":  r.get("dateReference", "").strip(),
                "data_entrega":     r.get("deliveryDate", "").strip(),
                "versao":           r.get("version", "").strip(),
                "link_documento":   r.get("urlSearch", "").strip(),
                "link_download":    r.get("urlDownload", "").strip(),
            })
        registros.extend(novos)

        filtro_info = f" [{len(novos)} das {len(resultados)} filtradas]" if filtrar_empresas else ""
        log.info(f"  {label} pag.{pagina}/{total_pags} +{len(novos)}{filtro_info} (total: {len(registros)})")

        if pagina >= total_pags:
            break

        pagina += 1
        time.sleep(SLEEP_ENTRE_PAGINAS)

    return registros


# ── Geração de janelas por trimestre ─────────────────────────────────────────

def gerar_janelas(ano_inicio: int, ano_fim: int) -> list[dict]:
    """
    Gera janelas mensais organizadas por trimestre.
    Cada janela tem no máximo 30 dias (limite da B3).
    """
    trimestres = {
        "Q1": [1, 2, 3],
        "Q2": [4, 5, 6],
        "Q3": [7, 8, 9],
        "Q4": [10, 11, 12],
    }

    janelas = []
    for ano in range(ano_inicio, ano_fim + 1):
        for trimestre, meses in trimestres.items():
            for mes in meses:
                ultimo_dia = calendar.monthrange(ano, mes)[1]
                inicio = date(ano, mes, 1)
                fim    = date(ano, mes, ultimo_dia)
                janelas.append({
                    "ano":        ano,
                    "trimestre":  trimestre,
                    "mes":        mes,
                    "inicio_iso": inicio.strftime("%Y-%m-%d"),
                    "fim_iso":    fim.strftime("%Y-%m-%d"),
                    "label":      f"[{trimestre} {ano}] {inicio.strftime('%b/%Y')}",
                })
    return janelas


# ── Interface do usuário ──────────────────────────────────────────────────────

def interface_usuario() -> tuple[int, int]:
    """Coleta ano início e ano fim via terminal."""
    print("\n+==========================================+")
    print("|     Scraper B3 - Fatos Relevantes        |")
    print("+==========================================+\n")

    while True:
        entrada = input("  Ano início [padrão: 2015]: ").strip()
        if not entrada:
            ano_inicio = 2015
            break
        try:
            ano_inicio = int(entrada)
            if 2000 <= ano_inicio <= 2025:
                break
            print("  ⚠ Digite um ano entre 2000 e 2025")
        except ValueError:
            print("  ⚠ Digite apenas o ano (ex: 2015)")

    while True:
        entrada = input("  Ano fim    [padrão: 2025]: ").strip()
        if not entrada:
            ano_fim = 2025
            break
        try:
            ano_fim = int(entrada)
            if ano_fim >= ano_inicio and ano_fim <= 2025:
                break
            print(f"  ⚠ Digite um ano entre {ano_inicio} e 2025")
        except ValueError:
            print("  ⚠ Digite apenas o ano (ex: 2025)")

    anos      = ano_fim - ano_inicio + 1
    trimestres = anos * 4
    janelas   = anos * 12

    print(f"\n  Calculando janelas de coleta...")
    print(f"  → {anos} ano(s) · {trimestres} trimestres · {janelas} janelas mensais")
    print(f"\n  Resumo:")
    print(f"    Período   : {ano_inicio} → {ano_fim}")
    print(f"    Trimestres: {trimestres}")
    print(f"    Estimativa: ~{round(janelas * 0.3)} min de coleta\n")

    confirma = input("  Confirmar e iniciar? (s/n): ").strip().lower()
    if confirma != 's':
        print("\n  Coleta cancelada.")
        sys.exit(0)

    return ano_inicio, ano_fim


# ── Scraper principal ─────────────────────────────────────────────────────────

def executar(ano_inicio: int, ano_fim: int, filtrar_empresas: bool = False, baixar_pdfs: bool = True):
    garantir_pastas()

    janelas   = gerar_janelas(ano_inicio, ano_fim)
    todos     = []
    total_jan = len(janelas)

    modo = "20 empresas do config" if filtrar_empresas else "todas as empresas"
    print(f"\n{'='*50}")
    print(f"  Modo: {modo}")
    print(f"{'='*50}")

    for i, janela in enumerate(janelas, 1):
        label = janela["label"]
        log.info(f"({i}/{total_jan}) {label}: {janela['inicio_iso']} -> {janela['fim_iso']}")

        registros = coletar_janela(janela["inicio_iso"], janela["fim_iso"], label,
                                   filtrar_empresas=filtrar_empresas)

        for r in registros:
            r["ano"]         = janela["ano"]
            r["trimestre"]   = janela["trimestre"]
            r["data_coleta"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")

        todos.extend(registros)
        time.sleep(0.3)

    # ── Salva CSV ─────────────────────────────────────────────────────────────
    if not todos:
        log.warning("Nenhum dado coletado")
        return

    df = pd.DataFrame(todos)
    df['empresa_original'] = df['empresa']
    df['empresa']          = df['empresa'].apply(normalizar_empresa)
    df.drop_duplicates(inplace=True)

    nome_csv = f"relatorios_{ano_inicio}_{ano_fim}.csv"
    csv_path = f"{PASTA_FATOS}/{nome_csv}"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    upload(csv_path, f"{S3_PREFIX_BRONZE_FATOS}/{nome_csv}")

    log.info(f"\n✓ CSV: {csv_path} ({len(df)} registros)")

    # ── Baixa PDFs ────────────────────────────────────────────────────────────
    pdfs_ok = 0
    if baixar_pdfs:
        log.info("\nBaixando PDFs...")
        for _, row in df.iterrows():
            url = row.get("link_download") or row.get("link_documento")
            if not url:
                continue
            pasta = Path(PASTA_PDFS) / normalizar_empresa(str(row.get("empresa", "GERAL")))
            pasta.mkdir(exist_ok=True)
            nome = nome_arquivo_pdf(
                str(row.get("empresa", "")),
                str(row.get("data_entrega", "")),
                str(row.get("categoria", ""))
            )
            if baixar_pdf(url, str(pasta / nome)):
                pdfs_ok += 1
                upload(str(pasta / nome), f"{S3_PREFIX_BRONZE_PDFS}/{pasta.name}/{nome}")

    print(f"\n{'='*50}")
    print(f"  CONCLUÍDO")
    print(f"  Total de registros : {len(df)}")
    print(f"  Empresas distintas : {df['empresa'].nunique()}")
    print(f"  PDFs baixados      : {pdfs_ok}")
    print(f"  CSV                : {csv_path}")
    print(f"{'='*50}\n")

# ── Wrapper para busca por empresa específica ─────────────────────────────────

def buscar_fatos_empresa(cvm_code: str, data_inicio: str, data_fim: str) -> list[dict]:
    """
    Busca fatos relevantes de uma empresa específica em um intervalo de datas.

    Parâmetros
    ----------
    cvm_code : str
        Código CVM da empresa (com ou sem zeros à esquerda).
    data_inicio : str
        Data inicial no formato YYYY-MM-DD.
    data_fim : str
        Data final no formato YYYY-MM-DD.

    Retorna
    -------
    list[dict]
        Lista de fatos relevantes da empresa no período, ou lista vazia se não houver.
        Retorna None somente em caso de erro de conexão irrecuperável.
    """
    cvm_limpo = str(cvm_code).strip().lstrip("0")
    label = f"[buscar_fatos_empresa/{cvm_limpo}] {data_inicio}→{data_fim}"

    try:
        todos = coletar_janela(data_inicio, data_fim, label, filtrar_empresas=False)
        return [r for r in todos if r.get("codigo_cvm", "").strip().lstrip("0") == cvm_limpo]
    except Exception as e:
        log.error(f"buscar_fatos_empresa({cvm_code}): erro de conexão — {e}")
        return None


# ── Atualização Incremental (AWS) ─────────────────────────────────────────────

def atualizar_recentes(dias: int = 7, filtrar_empresas: bool = True, baixar_pdfs: bool = True):
    """
    Busca apenas os fatos relevantes dos últimos N dias e faz o merge no CSV existente.
    """
    import datetime
    
    garantir_pastas()
    
    hoje = datetime.date.today()
    inicio = hoje - datetime.timedelta(days=dias)
    
    inicio_iso = inicio.strftime("%Y-%m-%d")
    fim_iso = hoje.strftime("%Y-%m-%d")
    
    label = f"Recentes ({dias} dias): {inicio_iso} a {fim_iso}"
    log.info(f"\nIniciando atualização incremental: {label}")
    
    registros = coletar_janela(inicio_iso, fim_iso, label, filtrar_empresas=filtrar_empresas)
    
    if not registros:
        log.info("Nenhum fato relevante novo encontrado nos últimos dias.")
        return
        
    for r in registros:
        r["ano"] = hoje.year
        r["trimestre"] = f"Q{(hoje.month-1)//3 + 1}"
        r["data_coleta"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")

    df_novos = pd.DataFrame(registros)
    df_novos['empresa_original'] = df_novos['empresa']
    df_novos['empresa'] = df_novos['empresa'].apply(normalizar_empresa)
    
    # Tenta ler o CSV mais recente existente
    csv_files = list(Path(PASTA_FATOS).glob("relatorios_*.csv"))
    if csv_files:
        latest_csv = sorted(csv_files)[-1]
        log.info(f"Fazendo merge com o arquivo existente: {latest_csv.name}")
        df_antigo = pd.read_csv(latest_csv)
        
        # Concatena e remove as duplicatas com base no link do documento e data
        df_combinado = pd.concat([df_antigo, df_novos], ignore_index=True)
        tamanho_antes = len(df_combinado)
        df_combinado.drop_duplicates(subset=["link_documento", "data_entrega", "empresa"], keep="last", inplace=True)
        
        novos_adicionados = len(df_combinado) - len(df_antigo)
        log.info(f"Registros novos adicionados: {novos_adicionados}")
        
        df_combinado.to_csv(latest_csv, index=False, encoding="utf-8-sig")
        upload(str(latest_csv), f"{S3_PREFIX_BRONZE_FATOS}/{latest_csv.name}")
        if novos_adicionados > 0:
            chaves_antigas = set(
                zip(df_antigo["link_documento"], df_antigo["data_entrega"], df_antigo["empresa"])
            )
            df_finais = df_combinado[
                ~df_combinado.apply(
                    lambda r: (r["link_documento"], r["data_entrega"], r["empresa"]) in chaves_antigas,
                    axis=1,
                )
            ]
        else:
            df_finais = pd.DataFrame()
    else:
        # Se não houver arquivo, cria um para o ano atual
        log.info("Nenhum CSV histórico encontrado. Criando um novo.")
        nome_csv = f"relatorios_{hoje.year}_{hoje.year}.csv"
        csv_path = Path(PASTA_FATOS) / nome_csv
        df_novos.drop_duplicates(inplace=True)
        df_novos.to_csv(csv_path, index=False, encoding="utf-8-sig")
        df_finais = df_novos
        upload(str(csv_path), f"{S3_PREFIX_BRONZE_FATOS}/{nome_csv}")

    # Baixar apenas PDFs novos (df_finais)
    if baixar_pdfs and not df_finais.empty:
        log.info(f"Tentando baixar {len(df_finais)} PDFs de fatos recentes...")
        pdfs_ok = 0
        for _, row in df_finais.iterrows():
            url = row.get("link_download") or row.get("link_documento")
            if not url: continue
            
            pasta = Path(PASTA_PDFS) / str(row.get("empresa", "GERAL"))
            pasta.mkdir(exist_ok=True)
            nome = nome_arquivo_pdf(str(row.get("empresa", "")), str(row.get("data_entrega", "")), str(row.get("categoria", "")))
            
            if baixar_pdf(url, str(pasta / nome)):
                pdfs_ok += 1
                upload(str(pasta / nome), f"{S3_PREFIX_BRONZE_PDFS}/{pasta.name}/{nome}")
        log.info(f"Concluído. {pdfs_ok} PDFs novos baixados.")



# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scraper B3 - Fatos Relevantes")
    parser.add_argument("--filtrar", action="store_true",
                        help="Coleta apenas as 20 empresas definidas no config.py")
    parser.add_argument("--sem-pdf", action="store_true",
                        help="Pula o download de PDFs pesados e gera apenas o CSV super rápido")
    args = parser.parse_args()

    ano_inicio, ano_fim = interface_usuario()
    executar(ano_inicio, ano_fim, filtrar_empresas=args.filtrar, baixar_pdfs=not args.sem_pdf)

