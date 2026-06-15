"""
gold_transform.py
=================
Lê dados Silver do S3 e grava tabelas Gold (Parquet enriquecido) no S3.

Uso:
    python pipeline/gold_transform.py                   # gera fatos_precos + resumo
    python pipeline/gold_transform.py --apenas-resumo
    python pipeline/gold_transform.py --apenas-fatos-precos
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
from pandas.tseries.offsets import BDay

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scraper.config import (
    EMPRESAS,
    S3_PREFIX_SILVER_FATOS,
    S3_PREFIX_SILVER_PRECOS,
    S3_PREFIX_GOLD_FATOS_PRECOS,
    S3_PREFIX_GOLD_RESUMO,
)
from storage.s3_manager import listar, ler_parquet, salvar_parquet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Carregar todos os Silver fatos ─────────────────────────────────────────────

def _carregar_todos_fatos() -> pd.DataFrame:
    """Lê todos os Parquets Silver de fatos e os concatena em um único DataFrame."""
    keys = listar(S3_PREFIX_SILVER_FATOS)
    parquet_keys = [k for k in keys if k.endswith(".parquet")]

    if not parquet_keys:
        log.warning("Nenhum Parquet Silver de fatos encontrado no S3.")
        return pd.DataFrame()

    frames = []
    for key in parquet_keys:
        df = ler_parquet(key)
        if not df.empty:
            frames.append(df)

    if not frames:
        log.warning("Todos os Parquets Silver de fatos estavam vazios.")
        return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True)

    # Normalizar codigo_cvm — remover zeros à esquerda
    if "codigo_cvm" in df_all.columns:
        df_all["codigo_cvm"] = df_all["codigo_cvm"].astype(str).str.strip().str.lstrip("0")

    # Garantir que data_entrega é datetime
    if "data_entrega" in df_all.columns:
        df_all["data_entrega"] = pd.to_datetime(df_all["data_entrega"], errors="coerce")

    log.info(f"Fatos Silver carregados: {len(df_all)} registros de {len(parquet_keys)} arquivo(s)")
    return df_all


# ── Gold: Fatos + Preços ───────────────────────────────────────────────────────

def gerar_fatos_precos():
    """Enriquece cada fato relevante com os preços d0, d-5 e d+5 (dias úteis).

    Usa pd.merge_asof (vetorizado) em vez de iterrows — ~100x mais rápido
    para 10 anos × 20 empresas de dados horários.
    """
    log.info("=== Gold: Fatos + Preços ===")

    df_fatos_all = _carregar_todos_fatos()

    if df_fatos_all.empty:
        log.error("Sem fatos Silver disponíveis. Abortando gerar_fatos_precos().")
        return

    tol = pd.Timedelta("14D")

    for empresa in EMPRESAS:
        ticker = empresa["ticker"]
        codigo_cvm = empresa["codigo_cvm"].lstrip("0")

        df_emp = df_fatos_all[df_fatos_all["codigo_cvm"] == codigo_cvm].copy()

        if df_emp.empty:
            log.warning(f"[{ticker}] Nenhum fato encontrado. Pulando.")
            continue

        log.info(f"[{ticker}] Processando {len(df_emp)} fato(s) (vetorizado)...")

        key_precos = f"{S3_PREFIX_SILVER_PRECOS}/{ticker}.parquet"
        df_precos = ler_parquet(key_precos)

        if df_precos.empty:
            log.warning(f"[{ticker}] Preços Silver não encontrados ({key_precos}). Pulando.")
            continue

        # Reduzir preços à granularidade diária (último Fechamento de cada dia)
        df_p = df_precos[["Datetime", "Fechamento"]].copy().sort_values("Datetime")
        df_p["_data"] = df_p["Datetime"].dt.date
        df_daily = (
            df_p.groupby("_data")["Fechamento"].last()
            .reset_index()
            .rename(columns={"_data": "_data"})
        )
        df_daily["_data"] = pd.to_datetime(df_daily["_data"]).astype("datetime64[s]")

        # Datas alvo em df_emp (tz-naive) — normaliza para segundos para compatibilidade com merge_asof
        df_emp["d0"]  = pd.to_datetime(df_emp["data_entrega"]).dt.tz_localize(None).astype("datetime64[s]")
        df_emp["dm5"] = (df_emp["d0"] - 5 * BDay()).astype("datetime64[s]")
        df_emp["dp5"] = (df_emp["d0"] + 5 * BDay()).astype("datetime64[s]")

        # merge_asof para d0
        df_emp = pd.merge_asof(
            df_emp.sort_values("d0"),
            df_daily.rename(columns={"_data": "d0", "Fechamento": "preco_d0"}),
            on="d0", direction="nearest", tolerance=tol,
        )

        # merge_asof para d-5
        df_emp = pd.merge_asof(
            df_emp.sort_values("dm5"),
            df_daily.rename(columns={"_data": "dm5", "Fechamento": "preco_d_menos5"}),
            on="dm5", direction="nearest", tolerance=tol,
        )

        # merge_asof para d+5
        df_emp = pd.merge_asof(
            df_emp.sort_values("dp5"),
            df_daily.rename(columns={"_data": "dp5", "Fechamento": "preco_d_mais5"}),
            on="dp5", direction="nearest", tolerance=tol,
        )

        # Variações vetorizadas (NaN propaga automaticamente quando preço ausente)
        df_emp["variacao_pct_antes"]  = (
            (df_emp["preco_d0"] - df_emp["preco_d_menos5"]) / df_emp["preco_d_menos5"] * 100
        )
        df_emp["variacao_pct_depois"] = (
            (df_emp["preco_d_mais5"] - df_emp["preco_d0"]) / df_emp["preco_d0"] * 100
        )
        df_emp["ticker"] = ticker
        df_emp = df_emp.drop(columns=["d0", "dm5", "dp5"])

        s3_key = f"{S3_PREFIX_GOLD_FATOS_PRECOS}/{ticker}.parquet"
        if salvar_parquet(df_emp, s3_key):
            log.info(f"[{ticker}] Gold fatos_precos salvo: {s3_key} ({len(df_emp)} registros)")
        else:
            log.error(f"[{ticker}] Falha ao salvar {s3_key}")


# ── Gold: Resumo por Empresa ───────────────────────────────────────────────────

def gerar_resumo():
    """Gera uma tabela-resumo com indicadores por empresa (preço atual, variação, contagem de fatos)."""
    log.info("=== Gold: Resumo por Empresa ===")

    hoje = pd.Timestamp.now(tz="America/Sao_Paulo")
    limite_7d  = hoje - pd.Timedelta(days=7)
    limite_30d = hoje - pd.Timedelta(days=30)

    df_fatos_all = _carregar_todos_fatos()

    rows = []

    for empresa in EMPRESAS:
        ticker    = empresa["ticker"]
        nome      = empresa["nome"]
        setor     = empresa["setor"]
        codigo_cvm = empresa["codigo_cvm"].lstrip("0")

        # Contagem de fatos desta empresa
        if not df_fatos_all.empty and "codigo_cvm" in df_fatos_all.columns:
            df_emp_fatos = df_fatos_all[df_fatos_all["codigo_cvm"] == codigo_cvm]
        else:
            df_emp_fatos = pd.DataFrame()

        fatos_7d  = 0
        fatos_30d = 0

        if not df_emp_fatos.empty and "data_entrega" in df_emp_fatos.columns:
            de = df_emp_fatos["data_entrega"]

            # Converter para timezone-aware para comparação com hoje (tz-aware)
            if de.dt.tz is None:
                de_aware = de.dt.tz_localize("America/Sao_Paulo", ambiguous="infer", nonexistent="shift_forward")
            else:
                de_aware = de.dt.tz_convert("America/Sao_Paulo")

            fatos_7d  = int((de_aware >= limite_7d).sum())
            fatos_30d = int((de_aware >= limite_30d).sum())

        # Ler preços Silver desta empresa
        key_precos = f"{S3_PREFIX_SILVER_PRECOS}/{ticker}.parquet"
        df_precos  = ler_parquet(key_precos)

        if df_precos.empty:
            log.warning(f"[{ticker}] Preços Silver não encontrados ou vazios. Adicionando linha com None.")
            rows.append({
                "ticker":              ticker,
                "nome":                nome,
                "setor":               setor,
                "ultimo_fechamento":   None,
                "ultimo_datetime":     None,
                "variacao_dia_pct":    None,
                "fatos_7d":            fatos_7d,
                "fatos_30d":           fatos_30d,
            })
            continue

        # Normaliza nome de coluna: aceita tanto "Datetime" (produção) quanto "datetime" (testes)
        if "Datetime" not in df_precos.columns and "datetime" in df_precos.columns:
            df_precos = df_precos.rename(columns={"datetime": "Datetime"})
        # Idem para Fechamento / close
        if "Fechamento" not in df_precos.columns and "close" in df_precos.columns:
            df_precos = df_precos.rename(columns={"close": "Fechamento"})

        df_precos = df_precos.sort_values("Datetime").reset_index(drop=True)

        ultimo_fechamento = float(df_precos["Fechamento"].iloc[-1])
        ultimo_datetime   = df_precos["Datetime"].iloc[-1]

        # Variação % em relação ao dia útil anterior
        # Agrupar por data, pegar os últimos 2 dias distintos
        df_precos_copia = df_precos.copy()
        df_precos_copia["_date"] = df_precos_copia["Datetime"].dt.date

        # Último fechamento por dia
        ultimos_por_dia = (
            df_precos_copia
            .sort_values("Datetime")
            .groupby("_date")["Fechamento"]
            .last()
            .sort_index()
        )

        if len(ultimos_por_dia) >= 2:
            fech_hoje_dia    = float(ultimos_por_dia.iloc[-1])
            fech_anterior    = float(ultimos_por_dia.iloc[-2])
            variacao_dia_pct = (fech_hoje_dia - fech_anterior) / fech_anterior * 100
        else:
            variacao_dia_pct = None

        rows.append({
            "ticker":              ticker,
            "nome":                nome,
            "setor":               setor,
            "ultimo_fechamento":   ultimo_fechamento,
            "ultimo_datetime":     ultimo_datetime,
            "variacao_dia_pct":    variacao_dia_pct,
            "fatos_7d":            fatos_7d,
            "fatos_30d":           fatos_30d,
        })

        log.info(f"[{ticker}] Resumo calculado — fechamento={ultimo_fechamento:.2f}, var_dia={variacao_dia_pct}")

    df_resumo = pd.DataFrame(rows)

    s3_key = f"{S3_PREFIX_GOLD_RESUMO}/resumo.parquet"
    if salvar_parquet(df_resumo, s3_key):
        log.info(f"Gold resumo salvo: {s3_key} ({len(df_resumo)} empresas)")
    else:
        log.error(f"Falha ao salvar {s3_key}")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Gold Transform — Silver Parquet → Gold Parquet no S3"
    )
    parser.add_argument(
        "--apenas-resumo",
        action="store_true",
        help="Gera apenas o resumo por empresa",
    )
    parser.add_argument(
        "--apenas-fatos-precos",
        action="store_true",
        help="Gera apenas a tabela fatos enriquecida com preços",
    )
    args = parser.parse_args()

    if args.apenas_resumo:
        gerar_resumo()
    elif args.apenas_fatos_precos:
        gerar_fatos_precos()
    else:
        gerar_fatos_precos()
        gerar_resumo()
