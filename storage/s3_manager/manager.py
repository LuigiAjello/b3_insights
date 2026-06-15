"""
manager.py
==========
Centraliza todas as operações S3 do projeto.
Retorna bool (sucesso/falha) e loga erros — nunca silencia exceções.
"""

import io
import logging
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
import pandas as pd

log = logging.getLogger(__name__)

_DEFAULT_BUCKET = os.environ.get("S3_BUCKET_NAME", "dados-b3-projeto")


def _client():
    return boto3.client("s3")


def upload(caminho_local: str, s3_key: str, bucket: str = _DEFAULT_BUCKET) -> bool:
    try:
        _client().upload_file(str(caminho_local), bucket, s3_key)
        log.info(f"[S3] upload ok → s3://{bucket}/{s3_key}")
        return True
    except Exception as e:
        log.warning(f"[S3] upload falhou → s3://{bucket}/{s3_key} — {e}")
        return False


def download(s3_key: str, caminho_local: str, bucket: str = _DEFAULT_BUCKET) -> bool:
    try:
        Path(caminho_local).parent.mkdir(parents=True, exist_ok=True)
        _client().download_file(bucket, s3_key, str(caminho_local))
        log.info(f"[S3] download ok ← s3://{bucket}/{s3_key}")
        return True
    except Exception as e:
        log.warning(f"[S3] download falhou ← s3://{bucket}/{s3_key} — {e}")
        return False


def existe(s3_key: str, bucket: str = _DEFAULT_BUCKET) -> bool:
    try:
        _client().head_object(Bucket=bucket, Key=s3_key)
        return True
    except ClientError:
        return False
    except Exception as e:
        log.warning(f"[S3] existe() falhou: s3://{bucket}/{s3_key} — {e}")
        return False


def listar(prefixo: str, bucket: str = _DEFAULT_BUCKET) -> list[str]:
    paginator = _client().get_paginator("list_objects_v2")
    keys: list[str] = []
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefixo):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except Exception as e:
        log.warning(f"[S3] listar() falhou: s3://{bucket}/{prefixo}* — {e}")
    return keys


def ler_parquet(s3_key: str, bucket: str = _DEFAULT_BUCKET) -> pd.DataFrame:
    try:
        resp = _client().get_object(Bucket=bucket, Key=s3_key)
        return pd.read_parquet(io.BytesIO(resp["Body"].read()))
    except Exception as e:
        log.warning(f"[S3] ler_parquet() falhou: s3://{bucket}/{s3_key} — {e}")
        return pd.DataFrame()


def salvar_parquet(df: pd.DataFrame, s3_key: str, bucket: str = _DEFAULT_BUCKET) -> bool:
    try:
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        buf.seek(0)
        _client().put_object(Bucket=bucket, Key=s3_key, Body=buf.read())
        log.info(f"[S3] salvar_parquet ok → s3://{bucket}/{s3_key}")
        return True
    except Exception as e:
        log.warning(f"[S3] salvar_parquet() falhou: s3://{bucket}/{s3_key} — {e}")
        return False
