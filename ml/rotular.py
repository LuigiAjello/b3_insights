"""
ml_rotular_pdfs.py
==================
Lê o TEXTO dos PDFs dos fatos (no S3) e usa a OpenAI (gpt-4o-mini) para rotular
sentimento / intensidade / materialidade de cada fato. Grava um cache parquet
(rotulos_llm.parquet) por fato_id, que o ml_loader passa a usar no lugar do
"chute por palavra-chave".

Uso:
    python ml_rotular_pdfs.py --teste 25      # dry-run barato
    python ml_rotular_pdfs.py --limite 1200   # rotula a amostra de treino
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import pandas as pd
import requests

from ml import loader as L

BUCKET = os.environ.get("S3_BUCKET_NAME", "b3insight-data")
_HDRS = {"User-Agent": "Mozilla/5.0"}
# caminho do cache de rótulos — configurável p/ rodar no container (env CACHE_ROTULOS)
CACHE = os.environ.get("CACHE_ROTULOS", "rotulos_llm.parquet")
MODELO_LLM = "gpt-4o-mini"
_s3 = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def _carregar_chave() -> str:
    chave = os.environ.get("OPENAI_API_KEY")
    if chave:
        return chave.strip()
    caminho = os.environ.get("OPENAI_ENV_FILE", "/Users/luigiajello/Desktop/ML- b3insigh/.env.openai")
    try:
        for linha in open(caminho):
            if linha.startswith("OPENAI_API_KEY="):
                return linha.split("=", 1)[1].strip()
    except OSError:
        pass
    raise RuntimeError("OPENAI_API_KEY não encontrada (env var ou .env.openai)")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


# --------------------------------------------------------------------------- #
# Fatos crus (precisamos de empresa + assunto, que o loader enxuto não devolve)
# --------------------------------------------------------------------------- #
def carregar_fatos_crus() -> pd.DataFrame:
    frames = [df for k in L._listar("silver/fatos/") if (df := L._ler_parquet(k)) is not None]
    df = pd.concat(frames, ignore_index=True)
    df["cvm_norm"] = df["codigo_cvm"].astype(str).str.strip().str.lstrip("0")
    df["ticker"] = df["cvm_norm"].map(L._CVM2TICKER)
    df = df[df["ticker"].notna()].copy()
    df["datahora_fato"] = L._to_naive(df["data_entrega"])
    df = df[df["datahora_fato"].notna()].copy()
    df["assunto"] = df["assunto"].fillna("").astype(str)
    df["empresa"] = df["empresa"].fillna("").astype(str)
    df["fato_id"] = df["link_documento"].fillna("").astype(str)
    df.loc[df["fato_id"] == "", "fato_id"] = (
        df["ticker"] + "_" + df["datahora_fato"].dt.strftime("%Y%m%d%H%M%S")
    )
    return df.drop_duplicates(subset="fato_id")


# --------------------------------------------------------------------------- #
# Índice de PDFs no S3:  filename = EMPRESA_DD-MM-AAAA_assunto.pdf
# --------------------------------------------------------------------------- #
def indexar_pdfs() -> pd.DataFrame:
    keys = []
    tok = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": "bronze/pdfs/"}
        if tok:
            kw["ContinuationToken"] = tok
        r = _s3.list_objects_v2(**kw)
        keys += [x["Key"] for x in r.get("Contents", []) if x["Key"].endswith(".pdf")]
        if not r.get("IsTruncated"):
            break
        tok = r["NextContinuationToken"]
    rows = []
    for k in keys:
        nome = k.split("/")[-1][:-4]
        m = re.match(r"(.+?)_(\d{2}-\d{2}-\d{4})_(.*)", nome)
        if not m:
            continue
        emp, data, slug = m.groups()
        rows.append({"key": k, "emp_norm": _norm(emp),
                     "data": data, "slug_norm": _norm(slug)})
    return pd.DataFrame(rows)


def achar_pdf(fato, idx: pd.DataFrame) -> str | None:
    data = pd.Timestamp(fato["datahora_fato"]).strftime("%d-%m-%Y")
    emp = _norm(fato["empresa"])
    cand = idx[(idx["data"] == data) & (idx["emp_norm"].str.startswith(emp[:6]))]
    if cand.empty:
        cand = idx[(idx["data"] == data) & (idx["emp_norm"].apply(lambda e: e[:6] == emp[:6]))]
    if cand.empty:
        return None
    alvo = _norm(fato["assunto"])
    if alvo:
        cand = cand.copy()
        cand["score"] = cand["slug_norm"].apply(
            lambda s: len(set(s[:20]) & set(alvo[:20])))
        cand = cand.sort_values("score", ascending=False)
    return cand.iloc[0]["key"]


def _texto_de_bytes(data: bytes, max_chars: int) -> str:
    try:
        import fitz  # pymupdf — parseia mesmo quando o Content-Type vem errado
        doc = fitz.open(stream=data, filetype="pdf")
        txt = "".join(p.get_text() for p in doc[:6])
        if txt.strip():
            return txt[:max_chars]
    except Exception:
        pass
    # fallback: documento veio como HTML
    try:
        import re as _re
        html = data.decode("utf-8", "ignore")
        return _re.sub(r"<[^>]+>", " ", html)[:max_chars]
    except Exception:
        return ""


def extrair_texto_url(url: str, max_chars: int = 6000) -> str:
    if not url:
        return ""
    try:
        r = requests.get(url, headers=_HDRS, timeout=30)
        if r.status_code != 200:
            return ""
        return _texto_de_bytes(r.content, max_chars)
    except Exception:
        return ""


def extrair_texto(key: str, max_chars: int = 6000) -> str:
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=key)
        return _texto_de_bytes(obj["Body"].read(), max_chars)
    except Exception:
        return ""


_PROMPT = (
    "Você é um analista do mercado de ações brasileiro. Leia o trecho de um Fato "
    "Relevante/Comunicado de uma empresa da B3 e classifique o provável impacto no "
    "preço da ação. Responda SOMENTE JSON com as chaves: "
    '"sentimento" (positivo|neutro|negativo), '
    '"intensidade" (leve|moderado|severo), '
    '"materialidade" (número 0 a 1, quão relevante para o preço). '
    "Considere materialidade alta para resultados, M&A, prejuízo, mudança de guidance; "
    "baixa para avisos burocráticos/administrativos."
)


def rotular_llm(client, assunto: str, texto: str) -> dict:
    import time as _t
    conteudo = f"ASSUNTO: {assunto}\n\nTEXTO:\n{texto}" if texto else f"ASSUNTO: {assunto}\n(sem texto extraído)"
    ultimo_erro = None
    for tentativa in range(5):  # backoff em 429/erros transitórios
        try:
            r = client.chat.completions.create(
                model=MODELO_LLM,
                response_format={"type": "json_object"},
                temperature=0,
                messages=[{"role": "system", "content": _PROMPT},
                          {"role": "user", "content": conteudo[:8000]}],
            )
            break
        except Exception as e:
            ultimo_erro = e
            _t.sleep(2 * (tentativa + 1))
    else:
        raise ultimo_erro
    d = json.loads(r.choices[0].message.content)
    sent = str(d.get("sentimento", "neutro")).lower()
    inten = str(d.get("intensidade", "leve")).lower()
    if sent not in ("positivo", "neutro", "negativo"):
        sent = "neutro"
    if inten not in ("leve", "moderado", "severo"):
        inten = "leve"
    try:
        mat = float(d.get("materialidade", 0.0))
    except (TypeError, ValueError):
        mat = 0.0
    return {"sentimento": sent, "intensidade": inten, "materialidade": max(0.0, min(1.0, mat))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--teste", type=int, default=0, help="rotula só N fatos (dry-run)")
    ap.add_argument("--limite", type=int, default=1200, help="amostra de treino a rotular")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI(api_key=_carregar_chave())

    # fato_ids exatamente da amostra de treino (só fatos com link/PDF)
    df_amostra, _, _, _ = L.carregar_dataframes(limite_fatos=args.limite, apenas_com_link=True)
    ids_alvo = set(df_amostra["fato_id"])
    crus = carregar_fatos_crus()
    crus = crus[crus["fato_id"].isin(ids_alvo)].reset_index(drop=True)
    if args.teste:
        crus = crus.head(args.teste)
    print(f">>> fatos a rotular: {len(crus)}")

    # cache existente (resume) — só conta como "feito" quem tem rótulo válido
    try:
        cache = pd.read_parquet(CACHE)
        feitos = set(cache.loc[cache.get("sentimento").notna(), "fato_id"]) if "sentimento" in cache else set()
    except Exception:
        cache = pd.DataFrame()
        feitos = set()
    pend = crus[~crus["fato_id"].isin(feitos)]
    print(f">>> já no cache: {len(feitos)} | pendentes: {len(pend)}")

    print(">>> indexando PDFs no S3...")
    idx = indexar_pdfs()
    print(f">>> {len(idx)} PDFs indexados")

    def processa(fato):
        # 1º tenta baixar o doc EXATO do fato (CVM); 2º fallback no PDF do S3
        texto = extrair_texto_url(fato.get("link_download", ""))
        fonte = "url" if texto else ""
        if not texto:
            key = achar_pdf(fato, idx)
            texto = extrair_texto(key) if key else ""
            fonte = "s3" if texto else "nenhuma"
        try:
            rot = rotular_llm(client, fato["assunto"], texto)
        except Exception as e:
            return {"fato_id": fato["fato_id"], "erro": str(e)[:80], "tem_texto": bool(texto)}
        rot.update({"fato_id": fato["fato_id"], "fonte_texto": fonte,
                    "tem_texto": bool(texto)})
        return rot

    def _flush(rows):
        if not rows:
            return
        base = pd.read_parquet(CACHE) if os.path.exists(CACHE) else pd.DataFrame()
        out = pd.concat([base, pd.DataFrame(rows)], ignore_index=True) if len(base) else pd.DataFrame(rows)
        out["_ok"] = out.get("sentimento").notna() if "sentimento" in out else False
        out = out.sort_values("_ok").drop_duplicates(subset="fato_id", keep="last").drop(columns="_ok")
        out.to_parquet(CACHE, index=False)

    todos, buffer = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(processa, f) for _, f in pend.iterrows()]
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result(); todos.append(r); buffer.append(r)
            if i % 50 == 0:                       # checkpoint incremental (resiliente a queda/sleep)
                _flush(buffer); buffer = []
                print(f"  ... {i}/{len(futs)} (checkpoint salvo)")
    _flush(buffer)

    df_novos = pd.DataFrame(todos)
    out = pd.read_parquet(CACHE)

    ok = df_novos[df_novos.get("sentimento").notna()] if "sentimento" in df_novos else pd.DataFrame()
    print("\n===== RESUMO ROTULAGEM =====")
    print(f"  rotulados agora : {len(df_novos)}  (cache total: {len(out)})")
    if len(ok):
        print(f"  c/ texto extr.  : {df_novos.get('tem_texto', pd.Series(dtype=bool)).sum()}/{len(df_novos)}")
        if "fonte_texto" in df_novos:
            print(f"  fonte do texto  : {df_novos['fonte_texto'].value_counts().to_dict()}")
        print(f"  sentimentos     : {ok['sentimento'].value_counts().to_dict()}")
        print(f"  intensidades    : {ok['intensidade'].value_counts().to_dict()}")
        print(f"  materialidade média: {ok['materialidade'].mean():.2f}")
    if "erro" in df_novos and df_novos["erro"].notna().any():
        print(f"  ERROS: {df_novos['erro'].dropna().value_counts().head(3).to_dict()}")


if __name__ == "__main__":
    main()
