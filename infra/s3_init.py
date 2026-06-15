"""
s3_init.py
==========
Cria o bucket S3 e materializa a estrutura Medallion com arquivos .keep.
Uso: python infra/s3_init.py [--bucket meu-bucket] [--region sa-east-1]
"""

import argparse
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PREFIXOS = [
    "bronze/fatos/",
    "bronze/precos/",
    "bronze/pdfs/",
    "silver/fatos/",
    "silver/precos/",
    "gold/fatos_precos/",
    "gold/resumo_empresa/",
]

def criar_bucket(client, bucket: str, region: str):
    # Verificar se já existe
    try:
        client.head_bucket(Bucket=bucket)
        log.info(f"Bucket já existe: s3://{bucket}")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "404":
            raise

    # Criar
    log.info(f"Criando bucket s3://{bucket} em {region}...")
    if region == "us-east-1":
        client.create_bucket(Bucket=bucket)
    else:
        client.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    log.info(f"✓ Bucket criado.")

def criar_estrutura(client, bucket: str):
    log.info("Criando estrutura de pastas (arquivos .keep)...")
    for prefixo in PREFIXOS:
        client.put_object(Bucket=bucket, Key=f"{prefixo}.keep", Body=b"")
        log.info(f"  ✓ {prefixo}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", default=os.environ.get("S3_BUCKET_NAME", "dados-b3-projeto"))
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "sa-east-1"))
    args = parser.parse_args()

    client = boto3.client("s3", region_name=args.region)
    criar_bucket(client, args.bucket, args.region)
    criar_estrutura(client, args.bucket)
    log.info(f"\n✓ s3://{args.bucket} pronto para uso.")
