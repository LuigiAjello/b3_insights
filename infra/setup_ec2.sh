#!/bin/bash
# setup_ec2.sh — Provisiona EC2 Ubuntu 22.04 para o Radar B3
# Uso: bash infra/setup_ec2.sh

set -e

REPO_URL="https://gitlab.com/pedro_miranda/projeto_cd_iv_pedro_luigi.git"
APP_DIR="/home/ubuntu/radar-b3"
SERVICE_FILE="/etc/systemd/system/radar-b3.service"

echo "=== [1/7] Atualizar apt e instalar dependências do sistema ==="
sudo apt-get update -y
sudo apt-get install -y python3.13 python3-pip git curl

echo "=== [2/7] Instalar uv ==="
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.cargo/bin:$PATH"

echo "=== [3/7] Clonar repositório ==="
if [ -d "$APP_DIR" ]; then
    echo "Diretório já existe, fazendo git pull..."
    git -C "$APP_DIR" pull origin main
else
    git clone "$REPO_URL" "$APP_DIR"
fi

echo "=== [4/7] Instalar dependências Python ==="
cd "$APP_DIR"
uv sync

echo "=== [5/7] Criar .env a partir do template ==="
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "⚠  Edite $APP_DIR/.env com os valores corretos antes de continuar."
    echo "    (Se usar IAM Role na EC2, apenas S3_BUCKET_NAME é obrigatório)"
fi

echo "=== [6/7] Instalar serviço systemd ==="
sudo cp "$APP_DIR/infra/radar-b3.service" "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable radar-b3

echo "=== [7/7] Iniciar serviço ==="
sudo systemctl start radar-b3

echo ""
echo "✓ Deploy concluído!"
echo ""
echo "Comandos úteis:"
echo "  sudo systemctl status radar-b3    # status do worker"
echo "  sudo journalctl -u radar-b3 -f    # logs em tempo real"
echo "  sudo systemctl restart radar-b3   # reiniciar"
