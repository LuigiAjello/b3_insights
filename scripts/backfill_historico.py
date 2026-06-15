"""
backfill_historico.py
=====================
Coleta histórico completo de 2025 até hoje (2026) para as 20 empresas do config.

Roda uma única vez antes de subir o worker no cloud.
Após concluir, o worker.py assume com sincronização de hora em hora.

Uso:
    python scripts/backfill_historico.py
"""

import sys
import logging
from pathlib import Path
from datetime import date

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scraper.scraper import executar, atualizar_recentes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("BACKFILL")


def main():
    hoje = date.today()

    log.info("=" * 60)
    log.info("ETAPA 1/2 — Histórico 2025 completo (20 empresas)")
    log.info("=" * 60)
    executar(ano_inicio=2025, ano_fim=2025, filtrar_empresas=True, baixar_pdfs=True)

    inicio_2026 = date(2026, 1, 1)
    dias_2026 = (hoje - inicio_2026).days + 1

    log.info("=" * 60)
    log.info(f"ETAPA 2/2 — Backfill 2026: Jan/2026 até {hoje} ({dias_2026} dias)")
    log.info("=" * 60)
    atualizar_recentes(dias=dias_2026, filtrar_empresas=True, baixar_pdfs=True)

    log.info("=" * 60)
    log.info("Backfill histórico concluído.")
    log.info("Agora suba o worker.py no cloud para sincronização horária.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
