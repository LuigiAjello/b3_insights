"""
fundamentalista.py
==================
Worker de dados fundamentalistas. Coleta indicadores (P/L, P/VP, EV/EBITDA,
ROE, margem, dívida/patrimônio, dividend yield) das empresas do config a partir
do fundamentus.com.br — UMA requisição traz todos os tickers — e grava no S3
(camadas silver e gold).

Uso:
    python scripts/fundamentalista.py
"""
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import pandas as pd
import requests

from scraper.config import EMPRESAS
from storage.s3_manager import salvar_parquet

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("FUNDAMENTALISTA")

URL = "https://www.fundamentus.com.br/resultado.php"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RadarB3/1.0)"}
S3_SILVER = "silver/fundamentalista"
S3_GOLD = "gold/fundamentalista"

# colunas % que vêm como texto "19,93%"
COLS_PCT = ["Div.Yield", "Mrg Bruta", "Mrg Ebit", "Mrg. Líq.", "ROIC", "ROE", "Cresc. Rec.5a"]
RENOMEAR = {
    "Papel": "ticker", "Cotação": "cotacao", "P/L": "pl", "P/VP": "pvp",
    "EV/EBITDA": "ev_ebitda", "ROE": "roe", "ROIC": "roic",
    "Mrg. Líq.": "margem_liquida", "Dív.Líq/ Patrim.": "div_liq_patrim",
    "Div.Yield": "div_yield", "Patrim. Líq": "patrimonio_liquido",
}


def _pct_para_float(s: pd.Series) -> pd.Series:
    """'19,93%' -> 19.93 ; '1.234,5%' -> 1234.5"""
    return pd.to_numeric(
        s.astype(str)
         .str.replace("%", "", regex=False)
         .str.replace(".", "", regex=False)
         .str.replace(",", ".", regex=False),
        errors="coerce",
    )


def coletar() -> pd.DataFrame:
    tickers = {e["ticker"] for e in EMPRESAS}
    log.info(f"Baixando fundamentus.com.br para {len(tickers)} tickers...")
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_html(io.StringIO(r.text), decimal=",", thousands=".")[0]

    df = df[df["Papel"].isin(tickers)].copy()
    for c in COLS_PCT:
        if c in df.columns:
            df[c] = _pct_para_float(df[c])
    df = df.rename(columns=RENOMEAR)
    df["data_coleta"] = pd.Timestamp.now().strftime("%Y-%m-%d")
    return df


def main() -> None:
    df = coletar()
    if df.empty:
        log.warning("Nenhum dado coletado — fundamentus pode estar indisponível.")
        return
    log.info(f"Coletados {len(df)} tickers.")
    salvar_parquet(df, f"{S3_SILVER}/fundamentalista.parquet")
    salvar_parquet(df, f"{S3_GOLD}/fundamentalista.parquet")
    log.info("✓ Salvo no S3 (silver + gold).")
    cols = [c for c in ["ticker", "pl", "pvp", "roe", "margem_liquida",
                        "ev_ebitda", "div_yield"] if c in df.columns]
    print(df[cols].to_string(index=False))


if __name__ == "__main__":
    main()
