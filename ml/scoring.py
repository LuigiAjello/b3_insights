"""
ml/scoring.py
=============
Carrega o modelo final (cascata porteiro + direção) e pontua uma matriz de
features. Mantido LEVE de propósito: depende só de pandas/numpy/joblib/xgboost —
não importa o motor de features (engine.py), então as telas do dashboard e o
job horário não pagam o custo do AR/ARIMA.

Origem do modelo (em ordem):
  1) S3  s3://<bucket>/deploy/ml/modelo_final.joblib   (modelo de produção)
  2) arquivo embutido na imagem (ml/artefatos_seed/modelo_final.joblib), se existir
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Optional

import boto3
import numpy as np
import pandas as pd

BUCKET = os.environ.get("S3_BUCKET_NAME", "b3insight-data")
REGIAO = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

from ml.config import FRACAO_TREINO  # noqa: F401  (fonte única do split temporal)

S3_MODELO = "deploy/ml/modelo_final.joblib"
SEED_LOCAL = Path(__file__).resolve().parent / "artefatos_seed" / "modelo_final.joblib"

_s3 = boto3.client("s3", region_name=REGIAO)
_cache: dict = {"bundle": None}


def carregar_bundle(forcar: bool = False) -> Optional[dict]:
    """Carrega o bundle do modelo (gate, direcao, feature_cols, ...) com cache."""
    if _cache["bundle"] is not None and not forcar:
        return _cache["bundle"]
    import joblib
    bundle = None
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=S3_MODELO)
        bundle = joblib.load(io.BytesIO(obj["Body"].read()))
    except Exception:
        if SEED_LOCAL.exists():
            bundle = joblib.load(SEED_LOCAL)
    _cache["bundle"] = bundle
    return bundle


def split_temporal(df: pd.DataFrame, coluna_data: str = "datahora_fato") -> pd.Series:
    """Marca cada linha como 'treino' (primeiros 85%) ou 'teste' (15% finais),
    respeitando a ordem temporal — exatamente o split do treino."""
    d = df.sort_values(coluna_data)
    n = len(d)
    i85 = int(FRACAO_TREINO * n)
    split = pd.Series("treino", index=d.index)
    split.iloc[i85:] = "teste"
    return split.reindex(df.index)


def pontuar(bundle: dict, df: pd.DataFrame) -> pd.DataFrame:
    """Roda o porteiro (mexeu?) e a direção (alta/queda) sobre as features.
    Devolve um DataFrame alinhado ao índice de `df`."""
    feats = bundle["feature_cols"]
    X = df.reindex(columns=feats).astype(float).fillna(0.0)

    prob_mexer = bundle["gate"].predict_proba(X)[:, 1]
    limiar = bundle.get("limiar_mexeu", 0.5)
    pred_mexeu = (prob_mexer >= limiar).astype(int)

    prob_alta = bundle["direcao"].predict_proba(X)[:, 1]
    classes = bundle.get("classes_direcao", {0: "queda", 1: "alta"})
    pred_dir = np.where(prob_alta >= 0.5, classes[1], classes[0])

    return pd.DataFrame(
        {
            "prob_mexer": prob_mexer.round(4),
            "pred_mexeu": pred_mexeu,
            "prob_alta": prob_alta.round(4),
            "pred_direcao": pred_dir,
        },
        index=df.index,
    )
