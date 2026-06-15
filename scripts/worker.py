import schedule
import time
import logging
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scraper.scraper import atualizar_recentes
from scripts.market_data import fetch_market_data
from scripts.fundamentalista import main as coletar_fundamentalista
from pipeline.silver_transform import transformar_fatos, transformar_precos
from pipeline.gold_transform import gerar_fatos_precos, gerar_resumo
from ml import pipeline as ml_pipeline

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger("WORKER")


def job_cotacoes():
    log.info("Iniciando rotina: Cotações → Bronze → Silver → Gold resumo")

    try:
        fetch_market_data()
        log.info("  ✓ Coleta de cotações concluída")
    except Exception as e:
        log.error(f"  ✗ Erro na coleta de cotações: {e}")
        return

    try:
        transformar_precos()
        log.info("  ✓ Silver preços atualizado")
    except Exception as e:
        log.error(f"  ✗ Erro no silver_transform (preços): {e}")

    try:
        gerar_resumo()
        log.info("  ✓ Gold resumo atualizado")
    except Exception as e:
        log.error(f"  ✗ Erro no gold_transform (resumo): {e}")


def job_fatos_recentes():
    log.info("Iniciando rotina: Fatos → Bronze → Silver → Gold fatos_precos")

    try:
        atualizar_recentes(dias=2, filtrar_empresas=True, baixar_pdfs=True)
        log.info("  ✓ Coleta de fatos concluída")
    except Exception as e:
        log.error(f"  ✗ Erro na coleta de fatos: {e}")
        return

    try:
        transformar_fatos()
        log.info("  ✓ Silver fatos atualizado")
    except Exception as e:
        log.error(f"  ✗ Erro no silver_transform (fatos): {e}")

    try:
        gerar_fatos_precos()
        log.info("  ✓ Gold fatos_precos atualizado")
    except Exception as e:
        log.error(f"  ✗ Erro no gold_transform (fatos_precos): {e}")


def job_fundamentalista():
    log.info("Iniciando rotina: Fundamentos (fundamentus) -> Silver/Gold")
    try:
        coletar_fundamentalista()
        log.info("  ✓ Fundamentos atualizados")
    except Exception as e:
        log.error(f"  ✗ Erro na coleta de fundamentos: {e}")


def job_ml_bootstrap():
    """LEVE — regenera os artefatos das telas (/treino, /prever) a partir do
    modelo + dataset já no S3. Roda de hora em hora."""
    log.info("Iniciando rotina: ML bootstrap (artefatos das telas)")
    try:
        ml_pipeline.bootstrap()
        log.info("  ✓ Artefatos de ML atualizados")
    except Exception as e:
        log.error(f"  ✗ Erro no ML bootstrap: {e}")


def job_ml_retrain():
    """PESADO — self-feeding semanal: rotula PDFs novos com IA, reconstrói o
    dataset (AR/ARIMA), re-treina a cascata e publica o modelo novo no S3."""
    log.info("Iniciando rotina: ML re-treino semanal (self-feeding)")
    try:
        ml_pipeline.retrain_semanal()
        log.info("  ✓ Re-treino semanal concluído")
    except Exception as e:
        log.error(f"  ✗ Erro no re-treino semanal: {e}")


if __name__ == "__main__":
    log.info("=== WORKER INICIADO ===")

    schedule.every(1).hours.do(job_cotacoes)
    schedule.every(1).hours.do(job_fatos_recentes)
    schedule.every(1).days.do(job_fundamentalista)   # fundamentos mudam devagar
    schedule.every(1).hours.do(job_ml_bootstrap)     # refresca telas de hora em hora
    schedule.every(7).days.do(job_ml_retrain)        # self-feeding semanal

    job_cotacoes()
    job_fatos_recentes()
    job_fundamentalista()
    job_ml_bootstrap()

    log.info("Agendamentos definidos. Entrando em loop de execução.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Worker finalizado pelo usuário.")
