# ── Dockerfile — Radar B3 ─────────────────────────────────────────────────────
# Imagem base: Python 3.13 slim (menor tamanho possível)
FROM python:3.13-slim

# Diretório de trabalho dentro do container
WORKDIR /app

# Instala o uv (gerenciador de pacotes)
RUN pip install --no-cache-dir uv

# Copia os arquivos de dependências primeiro (otimiza cache do Docker)
COPY pyproject.toml ./

# Resolve e instala as dependências (sem --frozen: o lock é regerado no build,
# pois adicionamos as libs de ML ao pyproject)
RUN uv sync

# Copia o restante do código (inclui ml/ e o modelo-semente embutido)
COPY . .

# Cria a pasta de dados local (caso precise de fallback sem S3)
RUN mkdir -p dados/raw/fatos dados/raw/precos dados/processed/fatos dados/processed/precos

# Variáveis de ambiente padrão (podem ser sobrescritas no docker run)
ENV S3_BUCKET_NAME=b3insight-data
ENV AWS_DEFAULT_REGION=us-east-1
ENV FLASK_PORT=5050

# Porta do dashboard Flask
EXPOSE 5050

# Comando padrão: sobe o dashboard Flask
# Para rodar o worker, sobrescreva com: docker run ... uv run python scripts/worker.py
CMD ["uv", "run", "python", "dashboard/server.py"]
