"""
server.py
=========
API Flask que substitui o Streamlit.
Serve o dashboard HTML estático e endpoints de dados.

Estratégia de dados: tenta S3 primeiro (Silver/Gold), cai para arquivos
locais se S3 estiver indisponível — permite desenvolvimento sem AWS.
"""

import io
import os
import sys
import json
import time
import datetime
import logging
from pathlib import Path

import boto3
import pandas as pd
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scraper.config import (
    EMPRESAS,
    S3_PREFIX_SILVER_FATOS, S3_PREFIX_SILVER_PRECOS,
    S3_PREFIX_BRONZE_FATOS, S3_PREFIX_GOLD_RESUMO,
)
from scraper.scraper import normalizar_empresa
from storage.s3_manager import listar as s3_listar, ler_parquet as s3_ler_parquet

PROC_DIR  = BASE_DIR / "dados" / "processed" / "precos"
FATOS_DIR = BASE_DIR / "dados" / "raw" / "fatos"
STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
CORS(app)


# ── Serialização compartilhada ────────────────────────────────────────────────

def _serializar_fatos(df_emp: pd.DataFrame) -> list[dict]:
    fatos = []
    for _, row in df_emp.iterrows():
        data_val = row.get("data_entrega")
        try:
            data_parsed = pd.to_datetime(data_val)
            data_fmt = data_parsed.strftime("%d/%m/%Y")
            ts = data_parsed.isoformat()
        except Exception:
            data_fmt = str(data_val)[:10]
            ts = None
        fatos.append({
            "data": data_fmt,
            "timestamp": ts,
            "titulo": str(row.get("assunto", "Fato Relevante")).strip(),
            "categoria": str(row.get("categoria", "")).strip(),
            "link": str(row.get("link_download", row.get("link_documento", ""))),
        })
    fatos.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return fatos


def _serializar_precos(df: pd.DataFrame, data_inicio, data_fim,
                        colunas_ptbr: bool) -> list[dict]:
    """Filtra e serializa DataFrame de preços para JSON (colunas EN ou pt-BR)."""
    if df.empty or "Datetime" not in df.columns:
        return []

    df = df.copy()
    df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True)
    df["_date"] = df["Datetime"].dt.date
    if data_inicio:
        df = df[df["_date"] >= data_inicio]
    if data_fim:
        df = df[df["_date"] <= data_fim]
    df = df.sort_values("Datetime")

    if colunas_ptbr:
        close_col, open_col, high_col, low_col = "Fechamento", "Abertura", "Maxima", "Minima"
    else:
        close_col, open_col, high_col, low_col = "Close", "Open", "High", "Low"

    if close_col not in df.columns:
        return []

    close = df[close_col]
    df_out = pd.DataFrame({
        "datetime": df["Datetime"].dt.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "close":    close.round(4),
        "open":     df[open_col].round(4) if open_col in df.columns else close.round(4),
        "high":     df[high_col].round(4) if high_col in df.columns else close.round(4),
        "low":      df[low_col].round(4)  if low_col  in df.columns else close.round(4),
        "volume":   df["Volume"].fillna(0).astype(int) if "Volume" in df.columns else 0,
    })
    return df_out.to_dict(orient="records")


# ── Helpers de dados ──────────────────────────────────────────────────────────

def obter_nome_pasta(cvm: str, nome_padrao: str) -> str:
    csv_files = list(FATOS_DIR.glob("relatorios_*.csv"))
    if csv_files:
        try:
            df = pd.read_csv(sorted(csv_files)[-1], dtype={"codigo_cvm": str},
                             usecols=["empresa", "codigo_cvm"])
            cvm_limpo = str(cvm).strip().lstrip("0")
            df["codigo_cvm"] = df["codigo_cvm"].astype(str).str.strip().str.lstrip("0")
            match = df[df["codigo_cvm"] == cvm_limpo]
            if not match.empty:
                return normalizar_empresa(match.iloc[0]["empresa"])
        except Exception:
            pass
    return normalizar_empresa(nome_padrao)


def carregar_fatos(codigo_cvm: str) -> list[dict]:
    cvm_limpo = str(codigo_cvm).strip().lstrip("0")

    # Tentar S3 (Silver Parquets)
    try:
        keys = [k for k in s3_listar(S3_PREFIX_SILVER_FATOS) if k.endswith(".parquet")]
        if keys:
            dfs = [df for key in keys for df in [s3_ler_parquet(key)] if not df.empty]
            if dfs:
                df_all = pd.concat(dfs, ignore_index=True)
                df_all["codigo_cvm"] = df_all["codigo_cvm"].astype(str).str.strip().str.lstrip("0")
                df_emp = df_all[df_all["codigo_cvm"] == cvm_limpo]
                if not df_emp.empty:
                    return _serializar_fatos(df_emp)
    except Exception as e:
        log.warning(f"S3 indisponível para fatos: {e}")

    # Fallback: CSV local
    csv_files = list(FATOS_DIR.glob("relatorios_*.csv"))
    if not csv_files:
        return []
    try:
        df = pd.read_csv(sorted(csv_files)[-1], dtype={"codigo_cvm": str})
        df["codigo_cvm"] = df["codigo_cvm"].astype(str).str.strip().str.lstrip("0")
        df_emp = df[df["codigo_cvm"] == cvm_limpo]
        return _serializar_fatos(df_emp)
    except Exception as e:
        log.error(f"Erro ao carregar fatos local: {e}")
        return []


def carregar_precos(ticker: str, data_inicio=None, data_fim=None) -> list[dict]:
    # Tentar S3 (Silver — colunas pt-BR)
    try:
        df = s3_ler_parquet(f"{S3_PREFIX_SILVER_PRECOS}/{ticker}.parquet")
        if not df.empty:
            return _serializar_precos(df, data_inicio, data_fim, colunas_ptbr=True)
    except Exception as e:
        log.warning(f"S3 indisponível para preços {ticker}: {e}")

    # Fallback: Parquet local (colunas EN originais do yfinance)
    parquet_path = PROC_DIR / f"{ticker}.parquet"
    if not parquet_path.exists():
        return []
    try:
        df = pd.read_parquet(parquet_path)
        return _serializar_precos(df, data_inicio, data_fim, colunas_ptbr=False)
    except Exception as e:
        log.error(f"Erro ao carregar preços local para {ticker}: {e}")
    return []


# ── Rotas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/api/empresas")
def api_empresas():
    return jsonify([
        {
            "ticker":     e["ticker"],
            "nome":       e["nome"],
            "setor":      e.get("setor", ""),
            "codigo_cvm": e.get("codigo_cvm", ""),
        }
        for e in EMPRESAS
    ])


@app.route("/api/precos/<ticker>")
def api_precos(ticker: str):
    ticker = ticker.upper()
    inicio_str = request.args.get("inicio")
    fim_str    = request.args.get("fim")

    try:
        data_inicio = datetime.date.fromisoformat(inicio_str) if inicio_str else None
        data_fim    = datetime.date.fromisoformat(fim_str)    if fim_str    else None
    except ValueError:
        data_inicio = data_fim = None

    return jsonify(carregar_precos(ticker, data_inicio, data_fim))


@app.route("/api/fatos/<codigo_cvm>")
def api_fatos(codigo_cvm: str):
    return jsonify(carregar_fatos(codigo_cvm))


GOLD_MODELO_KEY = "gold/modelo/classificacao.parquet"
_modelo_cache: dict = {"df": None}


def _carregar_modelo() -> pd.DataFrame:
    """Lê (e cacheia) a classificação do modelo da camada gold no S3."""
    if _modelo_cache["df"] is None:
        df = s3_ler_parquet(GOLD_MODELO_KEY)
        if not df.empty:
            _modelo_cache["df"] = df
        return df
    return _modelo_cache["df"]


@app.route("/modelo")
def modelo_page():
    return send_from_directory(str(STATIC_DIR), "modelo.html")


@app.route("/api/modelo")
def api_modelo():
    df = _carregar_modelo()
    if df.empty:
        return jsonify({"disponivel": False})

    com = df[df["target"].notna()].copy()
    com["mexeu"] = com["mexeu_v1"].astype(bool)
    n = len(com)

    dist = com["classificacao_v1"].value_counts().to_dict()
    por_ticker = (com.groupby("ticker")["mexeu"].mean().mul(100).round(1)
                  .sort_values(ascending=False))
    por_cat = (com.groupby("categoria")
               .agg(total=("mexeu", "size"),
                    pct=("mexeu", lambda s: round(s.mean() * 100, 1)))
               .sort_values("total", ascending=False).head(10)
               .reset_index())

    return jsonify({
        "disponivel":   True,
        "total":        int(n),
        "mexeram":      int(com["mexeu"].sum()),
        "pct_mexeram":  round(com["mexeu"].mean() * 100, 1),
        "distribuicao": {str(k): int(v) for k, v in dist.items()},
        "por_ticker":   {str(k): float(v) for k, v in por_ticker.items()},
        "por_categoria": por_cat.to_dict(orient="records"),
        "validacao":    {"razao_placebo": 2.5, "p_valor": "< 0,000001",
                         "metodo": "Event study (CAPM+ARIMA), janela 6 barras de pregão"},
    })


@app.route("/api/modelo/<codigo_cvm>")
def api_modelo_ticker(codigo_cvm: str):
    """Classificação dos fatos de uma empresa (resumo por ticker)."""
    df = _carregar_modelo()
    if df.empty:
        return jsonify({"disponivel": False})
    cvm = str(codigo_cvm).strip().lstrip("0")
    sub = df[df["codigo_cvm"].astype(str).str.strip().str.lstrip("0") == cvm]
    sub = sub[sub["target"].notna()]
    if sub.empty:
        return jsonify({"disponivel": True, "total": 0})
    return jsonify({
        "disponivel":  True,
        "total":       int(len(sub)),
        "mexeram":     int(sub["mexeu_v1"].sum()),
        "pct_mexeram": round(sub["mexeu_v1"].mean() * 100, 1),
        "alta":        int((sub["classificacao_v1"] == "alta").sum()),
        "queda":       int((sub["classificacao_v1"] == "queda").sum()),
    })


# ── Modelo ML (cascata) — telas /treino e /prever ──────────────────────────────
_BUCKET = os.environ.get("S3_BUCKET_NAME", "b3insight-data")
_REGIAO = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
_s3_ml = boto3.client("s3", region_name=_REGIAO)

K_TREINO_JSON = "gold/ml/treino.json"
K_DEMO_PARQUET = "gold/ml/demo_fatos.parquet"

_ml_cache: dict = {"demo": None, "demo_ts": 0.0, "treino": None, "treino_ts": 0.0}
_TTL = 60  # s — o worker regenera os artefatos de hora em hora


def _ler_json_s3(key: str) -> dict:
    try:
        obj = _s3_ml.get_object(Bucket=_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:
        log.warning(f"[ML] falha lendo {key}: {e}")
        return {}


def _carregar_treino() -> dict:
    if _ml_cache["treino"] is None or time.time() - _ml_cache["treino_ts"] > _TTL:
        _ml_cache["treino"] = _ler_json_s3(K_TREINO_JSON)
        _ml_cache["treino_ts"] = time.time()
    return _ml_cache["treino"]


def _carregar_demo() -> pd.DataFrame:
    if _ml_cache["demo"] is None or time.time() - _ml_cache["demo_ts"] > _TTL:
        try:
            obj = _s3_ml.get_object(Bucket=_BUCKET, Key=K_DEMO_PARQUET)
            _ml_cache["demo"] = pd.read_parquet(io.BytesIO(obj["Body"].read()))
        except Exception as e:
            log.warning(f"[ML] falha lendo demo_fatos: {e}")
            _ml_cache["demo"] = pd.DataFrame()
        _ml_cache["demo_ts"] = time.time()
    return _ml_cache["demo"]


def _fato_para_dict(row: pd.Series) -> dict:
    g = lambda k, d=None: (None if k not in row or pd.isna(row[k]) else row[k])
    return {
        "empresa": g("empresa"), "ticker": g("ticker"), "assunto": g("assunto"),
        "categoria": g("categoria"), "data": g("data"), "split": g("split"),
        "prob_mexer": float(g("prob_mexer") or 0.0),
        "pred_mexeu": int(g("pred_mexeu") or 0),
        "mexeu_real": int(g("mexeu_real") or 0),
        "acertou_mexeu": bool(g("acertou_mexeu")),
        "prob_alta": float(g("prob_alta") or 0.0),
        "pred_direcao": g("pred_direcao"),
        "direcao_real": g("direcao_real"),
        "sentimento_ord": (None if g("sentimento_ord") is None else int(g("sentimento_ord"))),
        "intensidade_ord": (None if g("intensidade_ord") is None else int(g("intensidade_ord"))),
        "materialidade": (None if g("materialidade") is None else round(float(g("materialidade")), 2)),
    }


@app.route("/treino")
def treino_page():
    return send_from_directory(str(STATIC_DIR), "treino.html")


@app.route("/prever")
def prever_page():
    return send_from_directory(str(STATIC_DIR), "prever.html")


@app.route("/api/treino")
def api_treino():
    t = _carregar_treino()
    if not t:
        return jsonify({"disponivel": False})
    t = {**t, "disponivel": True}
    return jsonify(t)


@app.route("/api/prever")
def api_prever():
    """Resumo + alguns exemplos reais (treino/teste) com previsão × resultado."""
    df = _carregar_demo()
    if df.empty:
        return jsonify({"disponivel": False})
    tr, te = df[df["split"] == "treino"], df[df["split"] == "teste"]

    def _resumo(sub):
        if sub.empty:
            return {"n": 0}
        return {"n": int(len(sub)),
                "acuracia": round(float(sub["acertou_mexeu"].mean()) * 100, 1),
                "movers_reais": int(sub["mexeu_real"].sum())}

    n = min(int(request.args.get("n", 10)), 50)
    exemplos = te.sample(min(n, len(te))) if len(te) else df.sample(min(n, len(df)))
    return jsonify({
        "disponivel": True,
        "resumo": {"treino": _resumo(tr), "teste": _resumo(te)},
        "exemplos": [_fato_para_dict(r) for _, r in exemplos.iterrows()],
    })


@app.route("/api/prever/sortear")
def api_prever_sortear():
    """Sorteia 1 fato (treino|teste) para a demonstração ao vivo."""
    df = _carregar_demo()
    if df.empty:
        return jsonify({"disponivel": False})
    split = request.args.get("split", "teste")
    sub = df[df["split"] == split]
    if sub.empty:
        sub = df
    return jsonify({"disponivel": True, "fato": _fato_para_dict(sub.sample(1).iloc[0])})


@app.route("/status")
def health_check():
    return "", 200


@app.route("/api/status")
def api_status():
    bronze_ok = any(k.endswith(".csv")     for k in s3_listar(S3_PREFIX_BRONZE_FATOS))
    silver_ok = any(k.endswith(".parquet") for k in s3_listar(S3_PREFIX_SILVER_FATOS))
    gold_ok   = any(k.endswith(".parquet") for k in s3_listar(S3_PREFIX_GOLD_RESUMO))

    ultima_atualizacao = None
    csv_files = list(FATOS_DIR.glob("relatorios_*.csv"))
    if csv_files:
        ultima_atualizacao = datetime.datetime.fromtimestamp(
            sorted(csv_files)[-1].stat().st_mtime
        ).strftime("%d/%m/%Y %H:%M")

    return jsonify({
        "status":             "ok",
        "bronze_ok":          bronze_ok,
        "silver_ok":          silver_ok,
        "gold_ok":            gold_ok,
        "ultima_atualizacao": ultima_atualizacao,
        "total_empresas":     len(EMPRESAS),
    })


if __name__ == "__main__":
    print("Dashboard iniciado em http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=True)
