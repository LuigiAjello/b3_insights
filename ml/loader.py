"""
ml_loader.py
============
Lê os dados REAIS do S3 (camadas silver/gold) e monta os 4 DataFrames no schema
exato que o pipeline_completo() do modelo_b3insight espera:

    df_fatos          : fato_id, ticker, datahora_fato, categoria, sentimento, intensidade, setor
    df_cotacoes       : ticker, datahora, preco_fechamento   (inclui IBOV)
    df_fundamentalista: ticker, trimestre, pl, pvp, ev_ebitda, roe, margem_liquida, divida_ebitda
    df_peers          : ticker, ticker_peer, correlacao_60d  (derivado dos preços)

Atritos tratados aqui (dados reais não vêm prontos pro modelo):
  - fatos da CVM não têm `sentimento`/`intensidade`  -> DERIVADOS por palavras-chave/heurística
  - `categoria` da CVM é texto livre               -> MAPEADA pras 7 categorias do modelo
  - coluna `ticker` dos fatos guarda o NOME          -> join por `codigo_cvm`
  - `peers` não existe no S3                          -> CALCULADO (correlação 60d dos preços)
  - fundamentos são snapshot único (sem histórico)   -> trimestre fixado p/ aplicar a todos os fatos

Uso:
    from ml_loader import carregar_dataframes
    df_fatos, df_cotacoes, df_fund, df_peers = carregar_dataframes(limite_fatos=800)
"""
from __future__ import annotations

import io
import os
import unicodedata
from typing import Optional, Tuple

import boto3
import numpy as np
import pandas as pd

BUCKET = os.environ.get("S3_BUCKET_NAME", "b3insight-data")
REGIAO = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# ---- Mapa oficial codigo_cvm <-> ticker <-> setor (de scraper/config.py) ----
EMPRESAS = [
    ("PETR4", "Energia",        "009512"), ("VALE3", "Mineração",      "004170"),
    ("ITUB4", "Finanças",       "019348"), ("BBDC4", "Finanças",       "000906"),
    ("BBAS3", "Finanças",       "001023"), ("ABEV3", "Consumo",        "023264"),
    ("MGLU3", "Varejo",         "022470"), ("WEGE3", "Indústria",      "005410"),
    ("EMBR3", "Aeroespacial",   "020087"), ("JBSS3", "Alimentos",      "080233"),
    ("SUZB3", "Papel/Celulose", "013986"), ("RENT3", "Serviços",       "019739"),
    ("TOTS3", "Tecnologia",     "019992"), ("LREN3", "Varejo",         "008133"),
    ("ELET3", "Energia",        "002437"), ("CSAN3", "Energia",        "019836"),
    ("RAIL3", "Logística",      "017450"), ("RDOR3", "Saúde",          "024821"),
    ("HAPV3", "Saúde",          "024392"), ("BRFS3", "Alimentos",      "016292"),
]
_CVM2TICKER = {cvm.lstrip("0"): tk for tk, _, cvm in EMPRESAS}
_TICKER2SETOR = {tk: setor for tk, setor, _ in EMPRESAS}

_s3 = boto3.client("s3", region_name=REGIAO)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sem_acento(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _ler_parquet(key: str) -> Optional[pd.DataFrame]:
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=key)
        return pd.read_parquet(io.BytesIO(obj["Body"].read()))
    except Exception:
        return None


def _listar(prefixo: str) -> list[str]:
    r = _s3.list_objects_v2(Bucket=BUCKET, Prefix=prefixo)
    return [x["Key"] for x in r.get("Contents", []) if x["Key"].endswith(".parquet")]


def _to_naive(serie: pd.Series) -> pd.Series:
    """Normaliza datetimes para horário de São Paulo SEM timezone (wall clock local)."""
    s = pd.to_datetime(serie, errors="coerce")
    try:
        if getattr(s.dt, "tz", None) is not None:
            return s.dt.tz_convert("America/Sao_Paulo").dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass
    return s


# --------------------------------------------------------------------------- #
# Derivação de sentimento / intensidade / categoria (o que falta nos fatos reais)
# --------------------------------------------------------------------------- #
_CATS = {
    "Resultado":   ["resultado", "demonstr", "dfp", "itr", "balanco", "trimestral", "release de resultado", "lucro liquido"],
    "M&A":         ["fusao", "aquisic", "incorporac", "reorganizac", "combinacao de negocios", "oferta publica", "opa", "alienac", "participacao societaria"],
    "Dividendos":  ["dividendo", "juros sobre capital", "jcp", "provento", "bonificac", "desdobramento", "grupamento", "recompra"],
    "Guidance":    ["guidance", "projec", "estimativa", "perspectiva", "plano de negocios"],
    "Regulatorio": ["cvm", "regulat", "multa", "autuac", "processo", "acao judicial", "aneel", "ans ", "anatel", "cade", "antitruste", "sancao", "fiscal"],
    "Governanca":  ["assembleia", "conselho", "administrac", "estatuto", "eleic", "governanc", "acordo de acionistas", "diretoria", "renuncia", "posse", "remunerac"],
}
_NEG = ["prejuizo", "queda", "reduc", "recuo", "perda", "multa", "processo", "rebaixa",
        "downgrade", "demiss", "fraude", "investigac", "atraso", "suspens", "deficit",
        "inadimpl", "recuperacao judicial", "impairment", "desvaloriz", "encerramento", "renuncia"]
_POS = ["lucro", "alta", "crescimento", "aumento", "recorde", "expans", "aprovac",
        "contrato", "aquisic", "dividendo", "jcp", "recompra", "upgrade", "melhora",
        "parceria", "investimento", "distribuic", "bonificac"]
_FORTE = ["recuperacao judicial", "fraude", "prejuizo", "aquisic", "fusao", "incorporac",
          "rebaixa", "multa", "impairment", "opa"]


def _map_categoria(categoria: str, assunto: str) -> str:
    t = _sem_acento(f"{categoria} {assunto}")
    for cat, kws in _CATS.items():
        if any(k in t for k in kws):
            return cat
    return "Operacional"


def _map_sentimento(categoria: str, assunto: str) -> str:
    t = _sem_acento(f"{assunto} {categoria}")
    score = sum(k in t for k in _POS) - sum(k in t for k in _NEG)
    return "positivo" if score > 0 else "negativo" if score < 0 else "neutro"


def _map_intensidade(cat_modelo: str, assunto: str, categoria: str) -> str:
    t = _sem_acento(f"{assunto} {categoria}")
    if any(k in t for k in _FORTE):
        return "severo"
    if cat_modelo in ("Resultado", "M&A", "Dividendos", "Regulatorio"):
        return "moderado"
    return "leve"


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #
def carregar_fatos(apenas_com_link: bool = False, rotulos_path: Optional[str] = None) -> pd.DataFrame:
    frames = [df for k in _listar("silver/fatos/") if (df := _ler_parquet(k)) is not None]
    if not frames:
        raise RuntimeError("Nenhum parquet em silver/fatos/")
    df = pd.concat(frames, ignore_index=True)

    df["cvm_norm"] = df["codigo_cvm"].astype(str).str.strip().str.lstrip("0")
    df["ticker"] = df["cvm_norm"].map(_CVM2TICKER)
    df = df[df["ticker"].notna()].copy()

    if apenas_com_link:  # só fatos com documento baixável (têm PDF pra IA ler)
        df = df[df["link_download"].fillna("").astype(str).str.startswith("http")].copy()

    df["datahora_fato"] = _to_naive(df["data_entrega"])
    df = df[df["datahora_fato"].notna()].copy()

    df["assunto"] = df["assunto"].fillna("").astype(str)
    df["categoria_raw"] = df["categoria"].fillna("").astype(str)
    df["categoria"] = [_map_categoria(c, a) for c, a in zip(df["categoria_raw"], df["assunto"])]
    df["sentimento"] = [_map_sentimento(c, a) for c, a in zip(df["categoria_raw"], df["assunto"])]
    df["intensidade"] = [_map_intensidade(cm, a, c)
                         for cm, a, c in zip(df["categoria"], df["assunto"], df["categoria_raw"])]
    df["setor"] = df["ticker"].map(_TICKER2SETOR)

    df["fato_id"] = df["link_documento"].fillna("").astype(str)
    df.loc[df["fato_id"] == "", "fato_id"] = (
        df["ticker"] + "_" + df["datahora_fato"].dt.strftime("%Y%m%d%H%M%S")
    )
    df = df.drop_duplicates(subset="fato_id").reset_index(drop=True)

    if rotulos_path and os.path.exists(rotulos_path):  # sobrescreve com rótulos da IA
        rot = pd.read_parquet(rotulos_path)
        rot = rot[rot.get("sentimento").notna()][["fato_id", "sentimento", "intensidade"]]
        df = df.merge(rot, on="fato_id", how="left", suffixes=("", "_llm"))
        df["sentimento"] = df["sentimento_llm"].fillna(df["sentimento"])
        df["intensidade"] = df["intensidade_llm"].fillna(df["intensidade"])

    return df[["fato_id", "ticker", "datahora_fato", "categoria",
               "sentimento", "intensidade", "setor"]]


def carregar_cotacoes() -> pd.DataFrame:
    linhas = []
    for k in _listar("silver/precos/"):
        ticker = k.split("/")[-1].replace(".parquet", "")
        df = _ler_parquet(k)
        if df is None or "Fechamento" not in df.columns:
            continue
        sub = pd.DataFrame({
            "ticker": ticker,
            "datahora": _to_naive(df["Datetime"]),
            "preco_fechamento": pd.to_numeric(df["Fechamento"], errors="coerce"),
        })
        linhas.append(sub.dropna())
    cot = pd.concat(linhas, ignore_index=True)
    return cot.sort_values(["ticker", "datahora"]).reset_index(drop=True)


def carregar_fundamentalista() -> pd.DataFrame:
    df = _ler_parquet("silver/fundamentalista/fundamentalista.parquet")
    if df is None:
        df = _ler_parquet("gold/fundamentalista/fundamentalista.parquet")
    if df is None:
        return pd.DataFrame(columns=["ticker", "trimestre", "pl", "pvp",
                                     "ev_ebitda", "roe", "margem_liquida", "divida_ebitda"])
    out = pd.DataFrame({
        "ticker": df["ticker"],
        # snapshot único -> data antiga p/ ficar "disponível" a todos os fatos (look-ahead leve, documentado)
        "trimestre": pd.Timestamp("2024-01-01"),
        "pl": pd.to_numeric(df.get("pl"), errors="coerce"),
        "pvp": pd.to_numeric(df.get("pvp"), errors="coerce"),
        "ev_ebitda": pd.to_numeric(df.get("ev_ebitda"), errors="coerce"),
        "roe": pd.to_numeric(df.get("roe"), errors="coerce"),
        "margem_liquida": pd.to_numeric(df.get("margem_liquida"), errors="coerce"),
        # fundamentus não dá Dív/EBITDA direto -> proxy por Dív.Líq/Patrim
        "divida_ebitda": pd.to_numeric(df.get("div_liq_patrim"), errors="coerce"),
    })
    return out.dropna(subset=["ticker"]).reset_index(drop=True)


def calcular_peers(df_cotacoes: pd.DataFrame, janela_dias: int = 60) -> pd.DataFrame:
    """Correlação dos retornos horários nos últimos `janela_dias` (exclui IBOV)."""
    cot = df_cotacoes[df_cotacoes["ticker"] != "IBOV"].copy()
    if cot.empty:
        return pd.DataFrame(columns=["ticker", "ticker_peer", "correlacao_60d"])
    corte = cot["datahora"].max() - pd.Timedelta(days=janela_dias)
    cot = cot[cot["datahora"] >= corte]
    piv = cot.pivot_table(index="datahora", columns="ticker", values="preco_fechamento")
    rets = np.log(piv / piv.shift(1)).dropna(how="all")
    corr = rets.corr()
    rows = []
    for t1 in corr.columns:
        for t2 in corr.columns:
            if t1 == t2 or pd.isna(corr.loc[t1, t2]):
                continue
            rows.append({"ticker": t1, "ticker_peer": t2,
                         "correlacao_60d": float(corr.loc[t1, t2])})
    return pd.DataFrame(rows)


def carregar_dataframes(
    limite_fatos: Optional[int] = None,
    tickers: Optional[list[str]] = None,
    espalhar: bool = True,
    apenas_com_link: bool = False,
    rotulos_path: Optional[str] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Monta os 4 DataFrames reais.
    limite_fatos: nº máximo de fatos. espalhar=True amostra UNIFORMEMENTE ao longo de todo
    o histórico (mantém a ordem temporal) — melhor p/ o split 70/15/15 do que pegar só o final.
    apenas_com_link=True: usa só fatos com documento baixável (necessário p/ a IA ler o PDF).
    rotulos_path: parquet com rótulos da IA (sobrescreve sentimento/intensidade).
    """
    df_cot = carregar_cotacoes()
    df_fatos = carregar_fatos(apenas_com_link=apenas_com_link, rotulos_path=rotulos_path)
    df_fund = carregar_fundamentalista()

    # só fatos de tickers que têm preço (senão AR seria NaN e o fato cai fora)
    tickers_com_preco = set(df_cot["ticker"].unique()) - {"IBOV"}
    if tickers:
        tickers_com_preco &= set(tickers)
    df_fatos = df_fatos[df_fatos["ticker"].isin(tickers_com_preco)].copy()
    df_fatos = df_fatos.sort_values("datahora_fato").reset_index(drop=True)
    if limite_fatos and len(df_fatos) > limite_fatos:
        if espalhar:
            idx = np.linspace(0, len(df_fatos) - 1, limite_fatos).round().astype(int)
            df_fatos = df_fatos.iloc[np.unique(idx)]
        else:
            df_fatos = df_fatos.tail(limite_fatos)
    df_fatos = df_fatos.reset_index(drop=True)

    df_peers = calcular_peers(df_cot)
    return df_fatos, df_cot, df_fund, df_peers


if __name__ == "__main__":
    f, c, fu, p = carregar_dataframes(limite_fatos=800)
    print("df_fatos          ", f.shape, "| cols:", list(f.columns))
    print("  categorias       ", f["categoria"].value_counts().to_dict())
    print("  sentimentos      ", f["sentimento"].value_counts().to_dict())
    print("  intensidades     ", f["intensidade"].value_counts().to_dict())
    print("  tickers          ", f["ticker"].nunique(), "| período:", f["datahora_fato"].min(), "->", f["datahora_fato"].max())
    print("df_cotacoes        ", c.shape, "| tickers:", c["ticker"].nunique())
    print("df_fundamentalista ", fu.shape)
    print("df_peers           ", p.shape)
