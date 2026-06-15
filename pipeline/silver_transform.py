"""
silver_transform.py
===================
Lê dados Bronze (CSV local ou S3) e grava Silver (Parquet limpo) no S3.

Uso:
    python pipeline/silver_transform.py             # processa fatos + precos
    python pipeline/silver_transform.py --apenas-fatos
    python pipeline/silver_transform.py --apenas-precos
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
import pytz

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scraper.config import (
    PASTA_FATOS,
    S3_PREFIX_BRONZE_FATOS,
    S3_PREFIX_SILVER_FATOS,
    S3_PREFIX_BRONZE_PRECOS,
    S3_PREFIX_SILVER_PRECOS,
)
from storage.s3_manager import download, listar, salvar_parquet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TZ_SP = pytz.timezone("America/Sao_Paulo")
RAW_PRECOS_DIR = BASE_DIR / "dados" / "raw" / "precos"


# ── Silver: Fatos ─────────────────────────────────────────────────────────────

def _silver_fatos_de_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if "codigo_cvm" in df.columns:
        df["codigo_cvm"] = df["codigo_cvm"].astype(str).str.strip().str.lstrip("0")

    for col in ["data_entrega", "data_referencia", "data_coleta"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], dayfirst=True, errors="coerce")

    dedup_cols = [c for c in ["link_documento", "data_entrega", "empresa"] if c in df.columns]
    if dedup_cols:
        antes = len(df)
        df.drop_duplicates(subset=dedup_cols, keep="last", inplace=True)
        log.info(f"  Dedup fatos: {antes} → {len(df)} registros")

    return df


def transformar_fatos():
    log.info("=== Silver: Fatos ===")
    csv_files = sorted(Path(PASTA_FATOS).glob("relatorios_*.csv"))

    if not csv_files:
        log.warning(f"Nenhum CSV em {PASTA_FATOS}. Tentando baixar do S3...")
        for key in listar(S3_PREFIX_BRONZE_FATOS):
            download(key, str(Path(PASTA_FATOS) / Path(key).name))
        csv_files = sorted(Path(PASTA_FATOS).glob("relatorios_*.csv"))

    if not csv_files:
        log.error("Nenhum CSV de fatos disponível. Abortando.")
        return

    for csv_path in csv_files:
        log.info(f"Processando: {csv_path.name}")
        try:
            df = pd.read_csv(csv_path, dtype={"codigo_cvm": str})
            silver_df = _silver_fatos_de_df(df)
            s3_key = f"{S3_PREFIX_SILVER_FATOS}/{csv_path.stem}.parquet"
            if salvar_parquet(silver_df, s3_key):
                log.info(f"  ✓ Silver gravado: {s3_key} ({len(silver_df)} registros)")
        except Exception as e:
            log.error(f"  Erro ao processar {csv_path.name}: {e}")


# ── Silver: Preços ────────────────────────────────────────────────────────────

def _silver_precos_de_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if "Datetime" in df.columns:
        df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True, errors="coerce")
        df["Datetime"] = df["Datetime"].dt.tz_convert(TZ_SP)
        df.drop_duplicates(subset=["Datetime"], keep="last", inplace=True)
        df.sort_values("Datetime", inplace=True)

    df = df.rename(columns={
        "Open": "Abertura",
        "High": "Maxima",
        "Low": "Minima",
        "Close": "Fechamento",
    })

    return df


def transformar_precos():
    log.info("=== Silver: Preços ===")
    RAW_PRECOS_DIR.mkdir(parents=True, exist_ok=True)
    csv_files = sorted(RAW_PRECOS_DIR.glob("*.csv"))

    if not csv_files:
        log.warning(f"Nenhum CSV em {RAW_PRECOS_DIR}. Tentando baixar do S3...")
        for key in listar(S3_PREFIX_BRONZE_PRECOS):
            download(key, str(RAW_PRECOS_DIR / Path(key).name))
        csv_files = sorted(RAW_PRECOS_DIR.glob("*.csv"))

    if not csv_files:
        log.error("Nenhum CSV de preços disponível. Abortando.")
        return

    for csv_path in csv_files:
        ticker = csv_path.stem
        log.info(f"Processando: {ticker}")
        try:
            df = pd.read_csv(csv_path)
            silver_df = _silver_precos_de_df(df)
            s3_key = f"{S3_PREFIX_SILVER_PRECOS}/{ticker}.parquet"
            if salvar_parquet(silver_df, s3_key):
                log.info(f"  ✓ Silver gravado: {s3_key} ({len(silver_df)} registros)")
        except Exception as e:
            log.error(f"  Erro ao processar {ticker}: {e}")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Silver Transform — Bronze CSV → Silver Parquet no S3")
    parser.add_argument("--apenas-fatos",  action="store_true", help="Processa apenas fatos relevantes")
    parser.add_argument("--apenas-precos", action="store_true", help="Processa apenas preços")
    args = parser.parse_args()

    if args.apenas_fatos:
        transformar_fatos()
    elif args.apenas_precos:
        transformar_precos()
    else:
        transformar_fatos()
        transformar_precos()
