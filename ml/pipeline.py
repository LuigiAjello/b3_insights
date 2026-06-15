"""
ml/pipeline.py
==============
Orquestra o ML rodando 100% na AWS (Fargate):

  bootstrap()        — LEVE. Regenera os artefatos das telas (demo + treino) a
                       partir do dataset/modelo já no S3. Roda de hora em hora e
                       no one-off de deploy. Não recomputa AR/ARIMA.

  retrain_semanal()  — PESADO. O "self-feeding": rotula com IA os PDFs de fatos
                       novos (gpt-4o-mini), reconstrói o dataset de features
                       (CAPM+ARIMA), re-treina a cascata, publica o modelo novo
                       no S3 e regenera os artefatos. Roda 1×/semana.

Chaves S3:
  deploy/ml/modelo_final.joblib    modelo de produção (lido pelo dashboard)
  deploy/ml/rotulos_llm.parquet    cache dos rótulos da IA (resume incremental)
  gold/ml/dataset_ml.parquet       matriz de features + target
  gold/ml/metrics_final.json       métricas honestas (holdout + CV temporal)
"""
from __future__ import annotations

import io
import json
import logging
import os
import tempfile

import boto3
import joblib
import numpy as np
import pandas as pd

from ml import artefatos

log = logging.getLogger("ML")

BUCKET = os.environ.get("S3_BUCKET_NAME", "b3insight-data")
REGIAO = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

K_MODELO = "deploy/ml/modelo_final.joblib"
K_ROTULOS = "deploy/ml/rotulos_llm.parquet"
K_DATASET = "gold/ml/dataset_ml.parquet"
K_METRICS = "gold/ml/metrics_final.json"

_s3 = boto3.client("s3", region_name=REGIAO)


# --------------------------------------------------------------------------- #
def bootstrap() -> None:
    """Regenera demo_fatos.parquet + treino.json a partir do que já está no S3."""
    log.info("[ML] bootstrap: regenerando artefatos das telas...")
    n, m = artefatos.gerar()
    log.info(f"[ML] bootstrap ok — demo={n} fatos, movers={m}")


# --------------------------------------------------------------------------- #
def _baixar_rotulos_para(path: str) -> int:
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=K_ROTULOS)
        with open(path, "wb") as fh:
            fh.write(obj["Body"].read())
        return len(pd.read_parquet(path))
    except Exception:
        return 0


def _rotular_ia(cache_path: str, limite: int = 4000, workers: int = 8) -> None:
    """Rotula com gpt-4o-mini os fatos ainda sem rótulo (best-effort)."""
    os.environ["CACHE_ROTULOS"] = cache_path
    from ml import rotular as R
    R.CACHE = cache_path
    from openai import OpenAI
    client = OpenAI(api_key=R._carregar_chave())

    df_amostra, _, _, _ = R.L.carregar_dataframes(limite_fatos=limite, apenas_com_link=True)
    ids_alvo = set(df_amostra["fato_id"])
    crus = R.carregar_fatos_crus()
    crus = crus[crus["fato_id"].isin(ids_alvo)].reset_index(drop=True)

    try:
        cache = pd.read_parquet(cache_path)
        feitos = set(cache.loc[cache.get("sentimento").notna(), "fato_id"]) if "sentimento" in cache else set()
    except Exception:
        feitos = set()
    pend = crus[~crus["fato_id"].isin(feitos)]
    log.info(f"[ML] rotulagem IA: {len(feitos)} no cache, {len(pend)} pendentes")
    if pend.empty:
        return

    idx = R.indexar_pdfs()

    def processa(fato):
        texto = R.extrair_texto_url(fato.get("link_download", ""))
        if not texto:
            key = R.achar_pdf(fato, idx)
            texto = R.extrair_texto(key) if key else ""
        try:
            rot = R.rotular_llm(client, fato["assunto"], texto)
        except Exception as e:
            return {"fato_id": fato["fato_id"], "erro": str(e)[:80]}
        rot["fato_id"] = fato["fato_id"]
        return rot

    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(processa, f) for _, f in pend.iterrows()]
        for fut in as_completed(futs):
            rows.append(fut.result())
    base = pd.read_parquet(cache_path) if os.path.exists(cache_path) else pd.DataFrame()
    out = pd.concat([base, pd.DataFrame(rows)], ignore_index=True) if len(base) else pd.DataFrame(rows)
    out["_ok"] = out.get("sentimento").notna() if "sentimento" in out else False
    out = out.sort_values("_ok").drop_duplicates(subset="fato_id", keep="last").drop(columns="_ok")
    out.to_parquet(cache_path, index=False)
    # sobe o cache atualizado de volta pro S3
    with open(cache_path, "rb") as fh:
        _s3.put_object(Bucket=BUCKET, Key=K_ROTULOS, Body=fh.read())
    log.info(f"[ML] rotulagem IA concluída — cache total {len(out)}")


def _reconstruir_dataset(rotulos_path: str) -> pd.DataFrame:
    """Recalcula AR (CAPM+ARIMA) + features sobre o silver atual do S3."""
    from ml import loader as ml_loader
    from ml import engine as mb
    log.info("[ML] reconstruindo dataset (AR/ARIMA — parte lenta)...")
    df_fatos, df_cot, df_fund, df_peers = ml_loader.carregar_dataframes(
        limite_fatos=None, apenas_com_link=True, rotulos_path=rotulos_path)
    df_fatos, df_cot, df_fund, df_peers = mb._normalizar_dataframes(
        df_fatos, df_cot, df_fund, df_peers)
    df_ar = mb.calcular_ar_combinado(df_cot, df_fatos)
    df_t = mb.calcular_threshold_dinamico(df_ar, df_fatos)
    df_t = mb.definir_target(df_t)
    setores_ref = sorted(df_fatos["setor"].unique().tolist())
    df_feat = mb.construir_features(df_t, df_ar, df_fund, df_peers,
                                    setores_referencia=setores_ref)
    if rotulos_path and os.path.exists(rotulos_path):
        rot = pd.read_parquet(rotulos_path)
        if "materialidade" in rot.columns:
            df_feat = df_feat.merge(rot[["fato_id", "materialidade"]], on="fato_id", how="left")
            df_feat["materialidade"] = df_feat["materialidade"].fillna(0.0)
    base = df_t[["fato_id", "datahora_fato", "target"]].dropna(subset=["target"]).copy()
    base["mexeu"] = (base["target"] != 0).astype(int)
    data = df_feat.merge(base, on="fato_id").sort_values("datahora_fato").reset_index(drop=True)
    buf = io.BytesIO(); data.to_parquet(buf, index=False); buf.seek(0)
    _s3.put_object(Bucket=BUCKET, Key=K_DATASET, Body=buf.read())
    log.info(f"[ML] dataset reconstruído: {data.shape} (mexeu={data['mexeu'].mean():.1%})")
    return data


def _treinar(D: pd.DataFrame) -> None:
    """Treina a cascata (porteiro regularizado + direção) e publica modelo+métricas."""
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import TimeSeriesSplit
    from xgboost import XGBClassifier

    D = D.sort_values("datahora_fato").reset_index(drop=True)
    FEATS = [c for c in D.columns if c not in {"fato_id", "datahora_fato", "target", "mexeu"}]

    def cv_roc(X, y, **kw):
        out = []
        for tr, te in TimeSeriesSplit(n_splits=4).split(X):
            if len(set(y.iloc[te])) < 2:
                continue
            m = XGBClassifier(eval_metric="logloss", n_jobs=4, random_state=7, **kw)
            m.fit(X.iloc[tr], y.iloc[tr])
            out.append(roc_auc_score(y.iloc[te], m.predict_proba(X.iloc[te])[:, 1]))
        return float(np.mean(out)), float(np.std(out))

    from ml.config import P1_PORTEIRO as P1, P2_DIRECAO as P2

    # estágio 1 — porteiro (regularizado)
    X, y = D[FEATS], D["mexeu"]
    n = len(D); i85 = int(.85 * n)
    spw = (y.iloc[:i85] == 0).sum() / max(int(y.iloc[:i85].sum()), 1)
    g = XGBClassifier(scale_pos_weight=spw, eval_metric="logloss", n_jobs=4, random_state=42, **P1)
    g.fit(X.iloc[:i85], y.iloc[:i85])
    roc_ho = roc_auc_score(y.iloc[i85:], g.predict_proba(X.iloc[i85:])[:, 1])
    cvm_, cvs_ = cv_roc(X, y, scale_pos_weight=spw, **P1)
    gate = XGBClassifier(scale_pos_weight=(y == 0).sum() / max(int(y.sum()), 1),
                         eval_metric="logloss", n_jobs=4, random_state=42, **P1).fit(X, y)

    # estágio 2 — direção (experimental)
    mov = D[D["target"] != 0].sort_values("datahora_fato").reset_index(drop=True)
    mov["dir"] = (mov["target"] == 1).astype(int)
    Xm, ym = mov[FEATS], mov["dir"]
    cvm2_, cvs2_ = cv_roc(Xm, ym, **P2)
    direcao = XGBClassifier(eval_metric="logloss", n_jobs=4, random_state=42, **P2).fit(Xm, ym)

    bundle = {"gate": gate, "direcao": direcao, "feature_cols": FEATS,
              "limiar_mexeu": 0.5, "classes_direcao": {0: "queda", 1: "alta"}}
    buf = io.BytesIO(); joblib.dump(bundle, buf); buf.seek(0)
    _s3.put_object(Bucket=BUCKET, Key=K_MODELO, Body=buf.read())

    metrics = {
        "estagio1_porteiro_binario": {
            "roc_holdout": round(float(roc_ho), 3),
            "roc_cv_temporal": f"{cvm_:.3f} ± {cvs_:.3f}",
            "interpretacao": "sinal REAL e estável; detecta SE o fato mexe",
        },
        "estagio2_direcao_EXPERIMENTAL": {
            "roc_cv_temporal": f"{cvm2_:.3f} ± {cvs2_:.3f}",
            "interpretacao": "BAIXA CONFIANÇA — direção não é prevista de forma confiável",
        },
        "n_fatos": int(n), "n_movers": int(len(mov)),
    }
    _s3.put_object(Bucket=BUCKET, Key=K_METRICS,
                   Body=json.dumps(metrics, ensure_ascii=False, indent=2).encode("utf-8"),
                   ContentType="application/json")
    log.info(f"[ML] modelo publicado. porteiro CV={metrics['estagio1_porteiro_binario']['roc_cv_temporal']}")


def retrain_semanal(rotular_ia: bool = True) -> None:
    log.info("=== [ML] RE-TREINO SEMANAL (self-feeding) ===")
    with tempfile.TemporaryDirectory() as tmp:
        rot_path = os.path.join(tmp, "rotulos_llm.parquet")
        n_cache = _baixar_rotulos_para(rot_path)
        log.info(f"[ML] cache de rótulos: {n_cache} fatos")
        if rotular_ia and os.environ.get("OPENAI_API_KEY"):
            try:
                _rotular_ia(rot_path)
            except Exception as e:
                log.warning(f"[ML] rotulagem IA falhou (segue com cache atual): {e}")
        else:
            log.info("[ML] rotulagem IA pulada (sem OPENAI_API_KEY)")
        D = _reconstruir_dataset(rot_path if os.path.exists(rot_path) else None)
        _treinar(D)
    bootstrap()
    log.info("=== [ML] RE-TREINO SEMANAL concluído ===")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    modo = sys.argv[1] if len(sys.argv) > 1 else "bootstrap"
    if modo == "retrain":
        retrain_semanal()
    else:
        bootstrap()
