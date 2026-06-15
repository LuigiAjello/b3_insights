"""
upload_s3.py
============
Faz upload de uma pasta local inteira para o bucket S3 do projeto.
Baseado no exemplo do professor — adaptado para o Radar B3.

No EC2 com IAM Role configurado, o boto3 pega as credenciais automaticamente.
Não é necessário informar chaves de acesso manualmente.

Uso:
    python scripts/upload_s3.py
"""

import boto3
import os
from pathlib import Path
from botocore.exceptions import ClientError

# ── Configuração ──────────────────────────────────────────────────────────────
# Pasta local que será enviada ao S3
PASTA_LOCAL = "./dados"

# Nome do bucket S3 (lê do .env ou usa o padrão)
NOME_BUCKET = os.environ.get("S3_BUCKET_NAME", "dados-b3-projeto")

# Prefixo opcional dentro do bucket (deixe "" para salvar na raiz)
PREFIXO_S3 = ""


# ── Função principal ──────────────────────────────────────────────────────────
def upload_folder_to_s3(local_directory: str, bucket_name: str, s3_prefix: str = ""):
    """
    Varre uma pasta local recursivamente e faz upload de todos os arquivos
    para o bucket S3, mantendo a estrutura de subpastas.

    No EC2 com IAM Role, o boto3 busca as credenciais automaticamente —
    não é necessário passar chaves de acesso no código.
    """
    s3_client = boto3.client("s3")

    local_path = Path(local_directory)
    if not local_path.exists():
        print(f"❌ Pasta '{local_directory}' não encontrada.")
        return

    print(f"📂 Iniciando upload: '{local_directory}' → s3://{bucket_name}/{s3_prefix}")
    print("-" * 60)

    total = 0
    erros = 0

    for arquivo in local_path.rglob("*"):
        if arquivo.is_file():
            # Caminho relativo para manter estrutura de pastas no S3
            caminho_relativo = arquivo.relative_to(local_path)

            # Define a chave no S3 (substitui barras do Windows)
            if s3_prefix:
                s3_key = f"{s3_prefix}/{caminho_relativo}".replace("\\", "/")
            else:
                s3_key = str(caminho_relativo).replace("\\", "/")

            try:
                s3_client.upload_file(str(arquivo), bucket_name, s3_key)
                print(f"  ✅ {caminho_relativo}")
                total += 1
            except ClientError as e:
                print(f"  ❌ {caminho_relativo}: {e}")
                erros += 1
            except Exception as e:
                print(f"  ❌ {caminho_relativo}: Erro inesperado — {e}")
                erros += 1

    print("-" * 60)
    print(f"✅ Upload finalizado: {total} arquivos enviados, {erros} erros.")


if __name__ == "__main__":
    upload_folder_to_s3(PASTA_LOCAL, NOME_BUCKET, PREFIXO_S3)
