import sys
import logging
from pathlib import Path
import pytz
import yfinance as yf
import pandas as pd
from datetime import datetime, date

# 1. Configurar paths
BASE_DIR = Path(__file__).resolve().parent.parent

# Garantir import do scraper
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from scraper.config import EMPRESAS, S3_PREFIX_BRONZE_PRECOS, S3_PREFIX_SILVER_PRECOS
    from storage.s3_manager import upload, salvar_parquet
except ImportError as e:
    logging.error(f"Erro ao importar EMPRESAS: {e}")
    sys.exit(1)

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

# Diretórios
RAW_DIR = BASE_DIR / "dados" / "raw" / "precos"
PROC_DIR = BASE_DIR / "dados" / "processed" / "precos"

RAW_DIR.mkdir(parents=True, exist_ok=True)
PROC_DIR.mkdir(parents=True, exist_ok=True)

TZ_SP = pytz.timezone("America/Sao_Paulo")


def fetch_market_data():
    for emp in EMPRESAS:
        ticker = emp["ticker"]
        ticker_yf = f"{ticker}.SA"
        csv_path = RAW_DIR / f"{ticker}.csv"
        parquet_path = PROC_DIR / f"{ticker}.parquet"

        log.info(f"Processando {ticker_yf}...")

        try:
            # Carga Incremental
            existing_df = pd.DataFrame()
            if csv_path.exists():
                existing_df = pd.read_csv(csv_path)
                existing_df['Datetime'] = pd.to_datetime(existing_df['Datetime'])
                
                # Normalizar Fuso Horário dos dados existentes
                if existing_df['Datetime'].dt.tz is None:
                    existing_df['Datetime'] = existing_df['Datetime'].dt.tz_localize(TZ_SP)
                else:
                    existing_df['Datetime'] = existing_df['Datetime'].dt.tz_convert(TZ_SP)

                last_date = existing_df['Datetime'].max()
                start_date = last_date.strftime('%Y-%m-%d')
                log.info(f"  Histórico encontrado. Buscando a partir de {start_date}.")
            else:
                start_date = "2025-01-01"
                log.info(f"  Sem histórico. Buscando a partir de {start_date}.")

            # Coleta yfinance
            b3_ticker = yf.Ticker(ticker_yf)
            
            # yfinance needs start/end for range or just history with period
            # For 1h interval, we can fetch up to 730 days.
            new_data = b3_ticker.history(start=start_date, interval="1h")

            if new_data.empty:
                log.info(f"  Sem novos dados para {ticker_yf}.")
                # Se não tem novos dados, mas já tinha existentes, salvamos o parquet se não houver
                if not existing_df.empty and not parquet_path.exists():
                    existing_df.to_parquet(parquet_path, index=False)
                continue

            # Processamento
            if new_data.index.name != 'Datetime':
                new_data.index.name = 'Datetime'
            new_data = new_data.reset_index()

            # Converter para o fuso local de São Paulo
            if new_data['Datetime'].dt.tz is None:
                new_data['Datetime'] = new_data['Datetime'].dt.tz_localize(TZ_SP)
            else:
                new_data['Datetime'] = new_data['Datetime'].dt.tz_convert(TZ_SP)

            # Combinação
            if not existing_df.empty:
                combined_df = pd.concat([existing_df, new_data], ignore_index=True)
                combined_df.drop_duplicates(subset=['Datetime'], keep='last', inplace=True)
            else:
                combined_df = new_data
                
            combined_df.sort_values(by='Datetime', inplace=True)

            # Salvar raw e processed localmente
            combined_df.to_csv(csv_path, index=False)
            combined_df.to_parquet(parquet_path, index=False)

            # Bronze: CSV bruto → S3
            upload(str(csv_path), f"{S3_PREFIX_BRONZE_PRECOS}/{ticker}.csv")

            # Silver: Parquet com colunas em pt-BR → S3
            silver_df = combined_df.rename(columns={
                "Open": "Abertura",
                "High": "Maxima",
                "Low": "Minima",
                "Close": "Fechamento",
            })
            salvar_parquet(silver_df, f"{S3_PREFIX_SILVER_PRECOS}/{ticker}.parquet")

            novos_registros = len(combined_df) - len(existing_df)
            log.info(f"  Atualização concluída: +{novos_registros} registros novos. Total: {len(combined_df)} linhas.")

        except Exception as e:
            log.error(f"  Erro ao buscar {ticker_yf}: {e}")

if __name__ == "__main__":
    fetch_market_data()
