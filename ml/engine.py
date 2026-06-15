"""
B3 Insight - Modelo de ML para previsao de reacao do mercado a fatos relevantes
==============================================================================

Pipeline completo (ETAPAS 1-8), modular, sem acesso a AWS:

    1. Calculo de Retorno Anormal (AR) via CAPM + ARIMA
    2. Definicao do target multiclasse com threshold dinamico por ticker
    3. Feature engineering sem leakage
    4. Split temporal cronologico (70/15/15)
    5. Treino do XGBoost multi:softprob com pesos balanceados
    6. Avaliacao (Macro F1, log loss, Brier, matriz de confusao, sanity check)
    7. Interpretabilidade (feature importance + SHAP)
    8. Funcao de inferencia para fatos novos

Uso tipico:

    from modelo_b3insight import pipeline_completo, prever_reacao, salvar_modelo

    artefatos = pipeline_completo(df_fatos, df_cotacoes, df_fundamentalista, df_peers)
    salvar_modelo(artefatos, "modelo_b3insight.joblib")

    resultado = prever_reacao(
        {"ticker": "PETR4", "datahora_fato": "2025-05-13 11:00",
         "categoria": "Resultado", "sentimento": "negativo",
         "intensidade": "severo", "setor": "Petroleo"},
        df_cotacoes_atualizado,
        artefatos,
    )
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
# matplotlib e shap só são usados nas funções de plot/SHAP (análise offline), não no
# caminho de produção (feature engineering + treino). Importação tolerante a ausência
# para a imagem do container não precisar carregá-los.
try:
    import matplotlib.pyplot as plt  # noqa: F401
except Exception:  # pragma: no cover
    plt = None
try:
    import shap  # noqa: F401
except Exception:  # pragma: no cover
    shap = None
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LinearRegression
from sklearn.metrics import (
    brier_score_loss,
    classification_report,
    confusion_matrix,
    f1_score,
    log_loss,
)
from sklearn.utils.class_weight import compute_class_weight

try:
    import pmdarima as pm  # type: ignore
except ImportError:
    pm = None
    warnings.warn(
        "pmdarima nao disponivel. ETAPA 1B (ARIMA) cairaa em modo degradado (AR_arima = NaN)."
    )


# =====================================================================
# CONFIGURACAO GLOBAL
# =====================================================================

RANDOM_STATE: int = 42
LOG_PREFIX: str = "[B3-INSIGHT]"

CATEGORIAS: List[str] = [
    "Resultado", "M&A", "Dividendos", "Guidance",
    "Regulatorio", "Governanca", "Operacional",
]
SENTIMENTO_MAP: Dict[str, int] = {"negativo": -1, "neutro": 0, "positivo": 1}
INTENSIDADE_MAP: Dict[str, int] = {"leve": 1, "moderado": 2, "severo": 3}
TENDENCIA_MAP: Dict[str, int] = {"caindo": -1, "estavel": 0, "subindo": 1}

# Multiplicador do desvio padrao para threshold dinamico
K_THRESHOLD: float = 1.5
# Janela de estimacao para CAPM/ARIMA, em dias corridos
JANELA_ESTIMACAO_DIAS: int = 60
# Lag de divulgacao fundamentalista (dias) — para evitar leakage de balanco nao publicado
LAG_DIVULGACAO_DIAS: int = 45


def log(msg: str) -> None:
    """Print padronizado com prefixo do projeto."""
    print(f"{LOG_PREFIX} {msg}")


# =====================================================================
# UTIL: normalizacao dos DataFrames de entrada
# =====================================================================

def _normalizar_dataframes(
    df_fatos: pd.DataFrame,
    df_cotacoes: pd.DataFrame,
    df_fundamentalista: pd.DataFrame,
    df_peers: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Garante tipos datetime, ordena e devolve copias. Idempotente."""
    df_fatos = df_fatos.copy()
    df_fatos["datahora_fato"] = pd.to_datetime(df_fatos["datahora_fato"])
    df_fatos = df_fatos.sort_values("datahora_fato").reset_index(drop=True)

    df_cotacoes = df_cotacoes.copy()
    df_cotacoes["datahora"] = pd.to_datetime(df_cotacoes["datahora"])
    df_cotacoes = df_cotacoes.sort_values(["ticker", "datahora"]).reset_index(drop=True)

    df_fundamentalista = df_fundamentalista.copy()
    df_fundamentalista["trimestre"] = pd.to_datetime(df_fundamentalista["trimestre"])

    df_peers = df_peers.copy()
    return df_fatos, df_cotacoes, df_fundamentalista, df_peers


# =====================================================================
# ETAPA 1 — RETORNO ANORMAL (AR)
# =====================================================================
# RISCO DE LEAKAGE (alto): a estimacao de alfa/beta (CAPM) e dos parametros
# do ARIMA precisa usar APENAS dados anteriores ao fato. O AR e a diferenca
# entre o retorno realizado APOS o fato e o retorno esperado pelos modelos
# ajustados com a janela pre-fato.


def _filtrar_cotacoes_anteriores(
    df_cotacoes: pd.DataFrame,
    ticker: str,
    datahora_fato: pd.Timestamp,
    janela_dias: int = JANELA_ESTIMACAO_DIAS,
) -> pd.DataFrame:
    """Cotacoes horarias do ticker em [dt_fato - janela_dias, dt_fato), estritamente anteriores."""
    inicio = datahora_fato - timedelta(days=janela_dias)
    mask = (
        (df_cotacoes["ticker"] == ticker)
        & (df_cotacoes["datahora"] >= inicio)
        & (df_cotacoes["datahora"] < datahora_fato)
    )
    return df_cotacoes.loc[mask].sort_values("datahora").reset_index(drop=True)


def _filtrar_cotacoes_posteriores(
    df_cotacoes: pd.DataFrame,
    ticker: str,
    datahora_fato: pd.Timestamp,
    n_horas: int,
) -> pd.DataFrame:
    """Primeiras `n_horas` cotacoes apos o fato (apenas horas existentes no pregao)."""
    mask = (df_cotacoes["ticker"] == ticker) & (df_cotacoes["datahora"] > datahora_fato)
    return df_cotacoes.loc[mask].sort_values("datahora").head(n_horas).reset_index(drop=True)


def _log_retornos(serie_preco: pd.Series) -> pd.Series:
    """ln(P_t / P_{t-1}). Remove o primeiro NaN."""
    return np.log(serie_preco / serie_preco.shift(1)).dropna()


def calcular_ar_capm(
    df_cotacoes: pd.DataFrame,
    ticker: str,
    datahora_fato: pd.Timestamp,
    horizontes_horas: Tuple[int, ...] = (1, 6, 24),
) -> Dict[int, float]:
    """
    Calcula AR cumulativo via Modelo de Mercado (CAPM com IBOV) para horizontes em horas.

    Args:
        df_cotacoes: DataFrame [ticker, datahora, preco_fechamento] (inclui ticker='IBOV').
        ticker: ticker da acao (diferente de 'IBOV').
        datahora_fato: timestamp do fato.
        horizontes_horas: horizontes para CAR (default (1, 6, 24)).

    Returns:
        Dict {horizonte: CAR_h}. NaN onde nao houver dado suficiente.
    """
    hist_acao = _filtrar_cotacoes_anteriores(df_cotacoes, ticker, datahora_fato)
    hist_ibov = _filtrar_cotacoes_anteriores(df_cotacoes, "IBOV", datahora_fato)

    if len(hist_acao) < 30 or len(hist_ibov) < 30:
        return {h: np.nan for h in horizontes_horas}

    df = pd.merge(
        hist_acao[["datahora", "preco_fechamento"]].rename(columns={"preco_fechamento": "p_acao"}),
        hist_ibov[["datahora", "preco_fechamento"]].rename(columns={"preco_fechamento": "p_ibov"}),
        on="datahora",
        how="inner",
    ).sort_values("datahora")

    if len(df) < 30:
        return {h: np.nan for h in horizontes_horas}

    df["r_acao"] = np.log(df["p_acao"] / df["p_acao"].shift(1))
    df["r_ibov"] = np.log(df["p_ibov"] / df["p_ibov"].shift(1))
    df = df.dropna()
    if len(df) < 20:
        return {h: np.nan for h in horizontes_horas}

    reg = LinearRegression().fit(df[["r_ibov"]].values, df["r_acao"].values)
    alfa, beta = float(reg.intercept_), float(reg.coef_[0])

    pos_acao = _filtrar_cotacoes_posteriores(df_cotacoes, ticker, datahora_fato, max(horizontes_horas))
    pos_ibov = _filtrar_cotacoes_posteriores(df_cotacoes, "IBOV", datahora_fato, max(horizontes_horas))
    if pos_acao.empty or pos_ibov.empty:
        return {h: np.nan for h in horizontes_horas}

    # Preco em t=0 (ultimo pre-fato) ancora os retornos pos-fato
    p0_acao = hist_acao["preco_fechamento"].iloc[-1]
    p0_ibov = hist_ibov["preco_fechamento"].iloc[-1]
    serie_acao = pd.concat([pd.Series([p0_acao]), pos_acao["preco_fechamento"].reset_index(drop=True)])
    serie_ibov = pd.concat([pd.Series([p0_ibov]), pos_ibov["preco_fechamento"].reset_index(drop=True)])

    r_acao_pos = np.log(serie_acao / serie_acao.shift(1)).dropna().reset_index(drop=True)
    r_ibov_pos = np.log(serie_ibov / serie_ibov.shift(1)).dropna().reset_index(drop=True)

    n_min = min(len(r_acao_pos), len(r_ibov_pos))
    r_acao_pos = r_acao_pos.iloc[:n_min]
    r_ibov_pos = r_ibov_pos.iloc[:n_min]
    ar_horario = r_acao_pos - (alfa + beta * r_ibov_pos)

    return {h: float(ar_horario.iloc[:h].sum()) if h <= len(ar_horario) else np.nan
            for h in horizontes_horas}


def calcular_ar_arima(
    df_cotacoes: pd.DataFrame,
    ticker: str,
    datahora_fato: pd.Timestamp,
    horizontes_horas: Tuple[int, ...] = (1, 6, 24),
) -> Dict[int, float]:
    """
    Calcula AR cumulativo via ARIMA univariado em log-retornos horarios.

    Trabalhamos em log-retornos (nao preco bruto) para garantir estacionariedade.
    auto_arima escolhe (p, d, q) automaticamente. Apenas dados < datahora_fato sao usados.
    """
    if pm is None:
        return {h: np.nan for h in horizontes_horas}

    hist = _filtrar_cotacoes_anteriores(df_cotacoes, ticker, datahora_fato)
    if len(hist) < 30:
        return {h: np.nan for h in horizontes_horas}

    log_ret_hist = _log_retornos(hist["preco_fechamento"])
    if len(log_ret_hist) < 20 or log_ret_hist.std() == 0:
        return {h: np.nan for h in horizontes_horas}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            modelo = pm.auto_arima(
                log_ret_hist.values,
                seasonal=False,
                suppress_warnings=True,
                error_action="ignore",
                stepwise=True,
                max_p=3,
                max_q=3,
                d=0,  # log-retornos geralmente ja sao estacionarios
            )
        except Exception:
            return {h: np.nan for h in horizontes_horas}

    n_pred = max(horizontes_horas)
    previsto = pd.Series(np.asarray(modelo.predict(n_periods=n_pred))).reset_index(drop=True)

    pos = _filtrar_cotacoes_posteriores(df_cotacoes, ticker, datahora_fato, n_pred)
    if pos.empty:
        return {h: np.nan for h in horizontes_horas}

    p0 = hist["preco_fechamento"].iloc[-1]
    serie = pd.concat([pd.Series([p0]), pos["preco_fechamento"].reset_index(drop=True)])
    r_real = np.log(serie / serie.shift(1)).dropna().reset_index(drop=True)

    n_min = min(len(r_real), len(previsto))
    ar_horario = r_real.iloc[:n_min] - previsto.iloc[:n_min]
    return {h: float(ar_horario.iloc[:h].sum()) if h <= len(ar_horario) else np.nan
            for h in horizontes_horas}


def calcular_ar_combinado(
    df_cotacoes: pd.DataFrame,
    df_fatos: pd.DataFrame,
    horizontes_horas: Tuple[int, ...] = (1, 6, 24),
) -> pd.DataFrame:
    """
    Para cada fato em df_fatos, calcula AR por CAPM e por ARIMA e devolve a media (ar_Xh).
    Tambem guarda os dois separadamente (ar_capm_Xh, ar_arima_Xh) para analise de robustez.
    """
    log(f"ETAPA 1 - Calculando AR (CAPM + ARIMA) para {len(df_fatos)} fatos...")
    n = len(df_fatos)
    registros: List[Dict[str, Any]] = []

    for i, row in enumerate(df_fatos.itertuples(index=False), 1):
        if n >= 10 and i % max(1, n // 10) == 0:
            log(f"  ... {i}/{n} fatos processados")

        ticker = row.ticker
        dt = pd.Timestamp(row.datahora_fato)
        capm = calcular_ar_capm(df_cotacoes, ticker, dt, horizontes_horas)
        arima = calcular_ar_arima(df_cotacoes, ticker, dt, horizontes_horas)

        reg: Dict[str, Any] = {"fato_id": row.fato_id}
        for h in horizontes_horas:
            reg[f"ar_capm_{h}h"] = capm.get(h, np.nan)
            reg[f"ar_arima_{h}h"] = arima.get(h, np.nan)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                reg[f"ar_{h}h"] = float(np.nanmean([capm.get(h, np.nan), arima.get(h, np.nan)]))
        registros.append(reg)

    df_ar = pd.DataFrame(registros)
    cobertura = df_ar["ar_6h"].notna().mean() * 100
    log(f"ETAPA 1 - AR calculado. Cobertura ar_6h: {cobertura:.1f}%")
    return df_ar


# =====================================================================
# ETAPA 1.5 — VALIDACAO DO AR (teste do placebo + event study)
# =====================================================================
# Verifica se o AR realmente captura o impacto dos fatos. Se em datas
# aleatorias (sem fato) o AR for proximo de zero E em datas com fato
# for significativamente diferente, a tese se sustenta.


def validar_ar_placebo(
    df_cotacoes: pd.DataFrame,
    df_fatos: pd.DataFrame,
    df_ar: pd.DataFrame,
    n_placebos: int = 200,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    Compara distribuicao de AR_6h em fatos reais vs datas aleatorias (placebo).

    Se o modelo de AR esta bem calibrado:
      - AR placebo (sem fato): media ~ 0, distribuicao centrada
      - AR real (com fato): media deslocada (se ha sinal nos fatos)

    Usa apenas CAPM no placebo (mais rapido que ARIMA, suficiente pra validar).

    Returns:
        Dict com estatisticas das duas distribuicoes + p-valor do t-test.
    """
    from scipy import stats as sp_stats  # import local pra evitar dep obrigatoria

    log(f"VALIDACAO - Gerando {n_placebos} datas placebo aleatorias...")
    rng = np.random.default_rng(seed)

    tickers = [t for t in df_cotacoes["ticker"].unique() if t != "IBOV"]
    dt_min = df_cotacoes["datahora"].min() + timedelta(days=70)  # garante histórico
    dt_max = df_cotacoes["datahora"].max() - timedelta(days=2)

    horas_validas = df_cotacoes[
        (df_cotacoes["datahora"] >= dt_min)
        & (df_cotacoes["datahora"] <= dt_max)
    ]["datahora"].unique()

    ar_placebos: List[float] = []
    for _ in range(n_placebos):
        ticker = rng.choice(tickers)
        dt = pd.Timestamp(rng.choice(horas_validas))
        ar = calcular_ar_capm(df_cotacoes, ticker, dt, horizontes_horas=(6,))
        if not np.isnan(ar.get(6, np.nan)):
            ar_placebos.append(ar[6])

    ar_reais = df_ar["ar_6h"].dropna().values

    media_placebo = float(np.mean(ar_placebos))
    std_placebo = float(np.std(ar_placebos))
    media_real = float(np.mean(np.abs(ar_reais)))  # magnitude (positivo ou negativo, tanto faz)
    media_real_sign = float(np.mean(ar_reais))

    # t-test: o |AR| dos fatos reais eh maior que o |AR| dos placebos?
    _, p_valor = sp_stats.ttest_ind(np.abs(ar_reais), np.abs(ar_placebos), equal_var=False)

    log("VALIDACAO - Resultado do teste do placebo:")
    log(f"  AR placebo (datas aleatorias, n={len(ar_placebos)}):")
    log(f"    media={media_placebo:+.5f}  std={std_placebo:.5f}")
    log(f"  AR real (fatos, n={len(ar_reais)}):")
    log(f"    media={media_real_sign:+.5f}  |AR| medio={media_real:.5f}")
    log(f"  p-valor (|AR_real| > |AR_placebo|): {p_valor:.4f}")
    if p_valor < 0.05:
        log("  TESE SUPORTADA: fatos provocam reacao significativamente maior que ruido")
    else:
        log("  TESE FRAGIL: |AR| em fatos nao difere significativamente do placebo")
        log("  Sugestoes: aumentar amostra, ajustar janela, ou rever classificacao do LLM")

    return {
        "ar_placebos": np.array(ar_placebos),
        "ar_reais": ar_reais,
        "media_placebo": media_placebo,
        "std_placebo": std_placebo,
        "media_real_abs": media_real,
        "media_real_signed": media_real_sign,
        "p_valor": float(p_valor),
        "tese_suportada": bool(p_valor < 0.05),
    }


def plotar_event_study(
    df_cotacoes: pd.DataFrame,
    df_fatos: pd.DataFrame,
    janela_pre: int = 6,
    janela_pos: int = 24,
    mostrar: bool = True,
) -> pd.DataFrame:
    """
    Gera o grafico classico de event study: AR cumulativo medio por hora ao redor do evento.

    (import lazy — só usado em análise offline, não no caminho de produção)

    Esperado:
      - Plano antes (t < 0): mercado nao sabia
      - Salto em t = 0: evento publicado
      - Apos t > 0: reacao persiste ou reverte

    Args:
        janela_pre: horas antes do evento (default 6).
        janela_pos: horas depois do evento (default 24).

    Returns:
        DataFrame com colunas [t, ar_medio, ar_se] (erro padrao).
    """
    log(f"VALIDACAO - Calculando event study (t = -{janela_pre}h a +{janela_pos}h)...")

    # Pra cada fato, calcula AR_t por hora individual (nao cumulativo)
    matriz: List[List[float]] = []
    for _, fato in df_fatos.iterrows():
        ticker = fato["ticker"]
        dt = pd.Timestamp(fato["datahora_fato"])

        # Janela total: [pre, pos]
        ar_por_t: List[float] = []
        for t in range(-janela_pre, janela_pos + 1):
            if t == 0:
                ar_por_t.append(np.nan)
                continue
            # AR cumulativo de 0 a t — usa CAPM (rapido)
            n_horas = abs(t)
            if t > 0:
                ar = calcular_ar_capm(df_cotacoes, ticker, dt, horizontes_horas=(n_horas,))
                ar_por_t.append(ar.get(n_horas, np.nan))
            else:
                # AR pre-evento: olha n_horas antes do fato (deveria ser proximo de zero)
                dt_anterior = dt - timedelta(hours=n_horas)
                ar = calcular_ar_capm(df_cotacoes, ticker, dt_anterior, horizontes_horas=(n_horas,))
                ar_por_t.append(ar.get(n_horas, np.nan))
        matriz.append(ar_por_t)

    arr = np.array(matriz, dtype=float)
    ts = list(range(-janela_pre, janela_pos + 1))
    ar_medio = np.nanmean(arr, axis=0)
    n_validos = np.sum(~np.isnan(arr), axis=0)
    ar_se = np.nanstd(arr, axis=0) / np.sqrt(np.maximum(n_validos, 1))

    df_es = pd.DataFrame({"t": ts, "ar_medio": ar_medio, "ar_se": ar_se})

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.axvline(0, color="red", linestyle="--", alpha=0.7, label="evento (t=0)")
    ax.axhline(0, color="gray", linestyle=":", alpha=0.5)
    ax.plot(ts, ar_medio, marker="o", linewidth=1.5)
    ax.fill_between(ts, ar_medio - 1.96 * ar_se, ar_medio + 1.96 * ar_se, alpha=0.2)
    ax.set_xlabel("Horas em torno do evento (0 = momento do fato)")
    ax.set_ylabel("AR cumulativo medio")
    ax.set_title("Event Study - reacao media do mercado ao redor do fato")
    ax.legend()
    ax.grid(alpha=0.3)
    plt.tight_layout()
    if mostrar:
        plt.show()
    else:
        plt.savefig("event_study.png", dpi=100)
        plt.close()
        log("  Grafico salvo em event_study.png")

    log(f"VALIDACAO - Event study: AR medio em t=+6h = {ar_medio[janela_pre + 6]:+.5f}")
    log(f"  AR medio em t=-6h  = {ar_medio[0]:+.5f}  (deveria estar proximo de 0)")
    return df_es


# =====================================================================
# ETAPA 2 — TARGET (threshold dinamico por ticker)
# =====================================================================
# RISCO DE LEAKAGE: sigma(AR_6h) por ticker tem que ser calculado de forma
# rolling — usando APENAS fatos com datahora anterior ao fato atual. Senao,
# o threshold "ja sabe" sobre eventos futuros.


def calcular_threshold_dinamico(
    df_ar: pd.DataFrame,
    df_fatos: pd.DataFrame,
    k: float = K_THRESHOLD,
) -> pd.DataFrame:
    """
    Calcula sigma_ar6h_historico (rolling sem leakage) e threshold = k * sigma para cada fato.

    Returns:
        df_fatos enriquecido com colunas sigma_ar6h_historico e threshold_ticker.
    """
    df = df_fatos.merge(df_ar[["fato_id", "ar_1h", "ar_6h", "ar_24h"]], on="fato_id", how="left")
    df = df.sort_values("datahora_fato").reset_index(drop=True)

    sigmas: List[float] = []
    for _, row in df.iterrows():
        ticker = row["ticker"]
        dt = row["datahora_fato"]
        passados = df[
            (df["ticker"] == ticker)
            & (df["datahora_fato"] < dt)
            & df["ar_6h"].notna()
        ]
        if len(passados) >= 5:
            sigmas.append(float(passados["ar_6h"].std()))
        else:
            # Fallback: sigma cross-section dos fatos anteriores em geral
            geral = df[(df["datahora_fato"] < dt) & df["ar_6h"].notna()]
            if len(geral) >= 20:
                sigmas.append(float(geral["ar_6h"].std()))
            else:
                sigmas.append(np.nan)

    df["sigma_ar6h_historico"] = sigmas
    df["threshold_ticker"] = k * df["sigma_ar6h_historico"]
    return df


def definir_target(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adiciona coluna 'target' em {-1, 0, +1}:
        +1 se ar_6h > threshold (alta anormal)
        -1 se ar_6h < -threshold (queda anormal)
         0 caso contrario (sem reacao)
    Linhas sem ar_6h ou threshold ficam com NaN e sao filtradas no split.
    """
    df = df.copy()

    def classifica(row: pd.Series) -> float:
        if pd.isna(row["ar_6h"]) or pd.isna(row["threshold_ticker"]):
            return np.nan
        if row["ar_6h"] > row["threshold_ticker"]:
            return 1
        if row["ar_6h"] < -row["threshold_ticker"]:
            return -1
        return 0

    df["target"] = df.apply(classifica, axis=1)
    dist = df["target"].value_counts(dropna=False).sort_index().to_dict()
    log(f"ETAPA 2 - Distribuicao do target: {dist}")
    return df


# =====================================================================
# ETAPA 3 — FEATURE ENGINEERING
# =====================================================================
# RISCO DE LEAKAGE (alto): TODA feature de historico/fundamentalista/setor
# eh calculada com strict `<` em datahora — nunca inclui o fato atual nem
# fatos posteriores. Fundamentalista usa lag de 45 dias para simular o
# atraso de divulgacao de balanco.


def _trimestre_disponivel(
    df_fundamentalista: pd.DataFrame, ticker: str, dt: pd.Timestamp,
    lag_dias: int = LAG_DIVULGACAO_DIAS,
) -> Optional[pd.Series]:
    """Trimestre mais recente cujos dados estavam publicos no momento do fato."""
    corte = dt - timedelta(days=lag_dias)
    sub = df_fundamentalista[
        (df_fundamentalista["ticker"] == ticker)
        & (df_fundamentalista["trimestre"] <= corte)
    ]
    if sub.empty:
        return None
    return sub.sort_values("trimestre").iloc[-1]


def _ultimos_n_trimestres(
    df_fundamentalista: pd.DataFrame, ticker: str, dt: pd.Timestamp,
    n: int = 3, lag_dias: int = LAG_DIVULGACAO_DIAS,
) -> pd.DataFrame:
    corte = dt - timedelta(days=lag_dias)
    sub = df_fundamentalista[
        (df_fundamentalista["ticker"] == ticker)
        & (df_fundamentalista["trimestre"] <= corte)
    ]
    return sub.sort_values("trimestre").tail(n)


def _classifica_tendencia(series: pd.Series) -> str:
    """Tres trimestres -> 'subindo' / 'caindo' / 'estavel' (ordinal posterior)."""
    serie = series.dropna()
    if len(serie) < 2:
        return "estavel"
    diffs = serie.diff().dropna()
    if (diffs > 0).all():
        return "subindo"
    if (diffs < 0).all():
        return "caindo"
    return "estavel"


def construir_features(
    df_fatos: pd.DataFrame,
    df_ar: pd.DataFrame,
    df_fundamentalista: pd.DataFrame,
    df_peers: pd.DataFrame,
    setores_referencia: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    Monta o vetor de features por fato. Saida: DataFrame com fato_id + N features numericas.

    setores_referencia: lista de setores conhecidos no treino. Se None, infere de df_fatos.
    Garantir o mesmo conjunto de colunas one-hot em treino e inferencia.
    """
    log("ETAPA 3 - Construindo features...")

    if "ar_6h" not in df_fatos.columns:
        df = df_fatos.merge(df_ar, on="fato_id", how="left")
    else:
        df = df_fatos.copy()
    df = df.sort_values("datahora_fato").reset_index(drop=True)

    setores = setores_referencia if setores_referencia is not None else sorted(df["setor"].unique().tolist())
    registros: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        ticker = row["ticker"]
        dt = pd.Timestamp(row["datahora_fato"])
        setor = row["setor"]
        feat: Dict[str, Any] = {"fato_id": row["fato_id"]}

        # ---- Do fato ----
        for c in CATEGORIAS:
            feat[f"cat_{c}"] = int(row["categoria"] == c)
        feat["sentimento_ord"] = SENTIMENTO_MAP.get(row["sentimento"], 0)
        feat["intensidade_ord"] = INTENSIDADE_MAP.get(row["intensidade"], 0)

        # ---- Timing do fato (capta diferenca entre fato no pregao vs after-market) ----
        hora = dt.hour
        dia_semana = dt.weekday()  # 0=segunda, 6=domingo
        eh_dia_util = dia_semana < 5
        feat["fato_durante_pregao"] = int(eh_dia_util and 10 <= hora < 17)
        feat["fato_apos_fechamento"] = int(eh_dia_util and (hora >= 17 or hora < 10))
        feat["fato_fim_de_semana"] = int(not eh_dia_util)
        feat["hora_do_dia"] = int(hora)

        # ---- Historico da empresa ----
        passados_sim = df[
            (df["ticker"] == ticker)
            & (df["datahora_fato"] < dt)
            & (df["categoria"] == row["categoria"])
            & (df["sentimento"] == row["sentimento"])
            & df["ar_6h"].notna()
        ]
        feat["historico_reacao_fatos_similares"] = (
            float(passados_sim["ar_6h"].mean()) if len(passados_sim) else 0.0
        )

        passados_neg = df[
            (df["ticker"] == ticker)
            & (df["datahora_fato"] < dt)
            & (df["datahora_fato"] >= dt - timedelta(days=365))
            & (df["sentimento"] == "negativo")
        ]
        feat["freq_fatos_negativos_12m"] = int(len(passados_neg))

        # ---- Fundamentalista (com lag de divulgacao) ----
        lf = _trimestre_disponivel(df_fundamentalista, ticker, dt)
        feat["pl"] = float(lf["pl"]) if lf is not None else np.nan
        feat["divida_ebitda"] = float(lf["divida_ebitda"]) if lf is not None else np.nan
        feat["roe"] = float(lf["roe"]) if lf is not None else np.nan
        feat["margem_liquida"] = float(lf["margem_liquida"]) if lf is not None else np.nan

        ult3 = _ultimos_n_trimestres(df_fundamentalista, ticker, dt, n=3)
        feat["margem_tendencia_ord"] = TENDENCIA_MAP[
            _classifica_tendencia(ult3["margem_liquida"]) if not ult3.empty else "estavel"
        ]

        # Comparativo setorial — mediana dos tickers do mesmo setor disponiveis na data
        tickers_setor = df.loc[df["setor"] == setor, "ticker"].unique()
        pls, roes, dividas = [], [], []
        for t in tickers_setor:
            lf_peer = _trimestre_disponivel(df_fundamentalista, t, dt)
            if lf_peer is not None:
                pls.append(lf_peer["pl"])
                roes.append(lf_peer["roe"])
                dividas.append(lf_peer["divida_ebitda"])
        med_pl = float(np.median(pls)) if pls else np.nan
        med_roe = float(np.median(roes)) if roes else np.nan
        med_div = float(np.median(dividas)) if dividas else np.nan
        feat["pl_vs_setor"] = (
            feat["pl"] - med_pl if not (np.isnan(med_pl) or np.isnan(feat["pl"])) else 0.0
        )
        feat["roe_vs_setor"] = (
            feat["roe"] - med_roe if not (np.isnan(med_roe) or np.isnan(feat["roe"])) else 0.0
        )
        feat["divida_vs_setor"] = (
            feat["divida_ebitda"] - med_div
            if not (np.isnan(med_div) or np.isnan(feat["divida_ebitda"]))
            else 0.0
        )

        # ---- Setor one-hot (vocabulario fixo) ----
        for s in setores:
            feat[f"setor_{s}"] = int(setor == s)

        # ---- Contagio peers ----
        peers_rel = df_peers[
            (df_peers["ticker"] == ticker) & (df_peers["correlacao_60d"] > 0.6)
        ]["ticker_peer"].tolist()
        if peers_rel:
            neg_peer = df[
                (df["ticker"].isin(peers_rel))
                & (df["datahora_fato"] >= dt - timedelta(hours=24))
                & (df["datahora_fato"] < dt)
                & (df["sentimento"] == "negativo")
            ]
            feat["fato_negativo_peer_24h"] = int(len(neg_peer) > 0)
        else:
            feat["fato_negativo_peer_24h"] = 0
        corrs = df_peers[df_peers["ticker"] == ticker]["correlacao_60d"]
        feat["correlacao_media_peers"] = float(corrs.mean()) if len(corrs) else 0.0

        # ---- Event Study (calculados com fatos anteriores) ----
        cat_passados = df[
            (df["categoria"] == row["categoria"])
            & (df["datahora_fato"] < dt)
            & df["ar_1h"].notna()
            & df["ar_6h"].notna()
            & df["ar_24h"].notna()
        ]
        if len(cat_passados) >= 5:
            ratios = (
                cat_passados["ar_1h"].abs() / cat_passados["ar_6h"].abs().replace(0, np.nan)
            ).dropna()
            feat["velocidade_media_categoria"] = float(ratios.mean()) if len(ratios) else 0.5
            recup = (cat_passados["ar_24h"] / cat_passados["ar_6h"].replace(0, np.nan)).dropna()
            feat["recuperacao_media_categoria"] = float(recup.mean()) if len(recup) else 1.0
        else:
            feat["velocidade_media_categoria"] = 0.5
            feat["recuperacao_media_categoria"] = 1.0

        passados_ticker = df[
            (df["ticker"] == ticker)
            & (df["datahora_fato"] < dt)
            & df["ar_6h"].notna()
        ]
        feat["volatilidade_historica_ar"] = (
            float(passados_ticker["ar_6h"].std()) if len(passados_ticker) >= 3 else 0.0
        )

        registros.append(feat)

    df_feat = pd.DataFrame(registros).fillna(0.0)
    log(f"ETAPA 3 - Dataset de features: {df_feat.shape[0]} linhas x {df_feat.shape[1] - 1} features")
    return df_feat


# =====================================================================
# ETAPA 4 — SPLIT TEMPORAL (sem leakage)
# =====================================================================
# RISCO DE LEAKAGE: NUNCA usar train_test_split aleatorio. O split eh
# cronologico: treino primeiro, depois validacao, depois teste.


@dataclass
class SplitTemporal:
    X_train: pd.DataFrame
    X_val: pd.DataFrame
    X_test: pd.DataFrame
    y_train: pd.Series
    y_val: pd.Series
    y_test: pd.Series
    ids_train: pd.Series
    ids_val: pd.Series
    ids_test: pd.Series
    feature_cols: List[str]


def split_temporal(
    df_features: pd.DataFrame,
    df_fatos: pd.DataFrame,
    proporcoes: Tuple[float, float, float] = (0.70, 0.15, 0.15),
) -> SplitTemporal:
    """Split cronologico 70/15/15. Filtra fatos com target NaN. Loga datas e distribuicoes."""
    assert abs(sum(proporcoes) - 1.0) < 1e-6, "Proporcoes precisam somar 1."
    df = df_features.merge(
        df_fatos[["fato_id", "datahora_fato", "target"]], on="fato_id", how="left"
    )
    df = df.dropna(subset=["target"]).sort_values("datahora_fato").reset_index(drop=True)

    n = len(df)
    n_tr = int(n * proporcoes[0])
    n_va = int(n * proporcoes[1])

    feature_cols = [c for c in df.columns if c not in ("fato_id", "datahora_fato", "target")]

    parts = {
        "Treino": df.iloc[:n_tr],
        "Validacao": df.iloc[n_tr:n_tr + n_va],
        "Teste": df.iloc[n_tr + n_va:],
    }
    log("ETAPA 4 - Split temporal:")
    for nome, sub in parts.items():
        if len(sub):
            log(
                f"  {nome:10s}: {len(sub):4d} fatos | "
                f"{sub['datahora_fato'].min()} -> {sub['datahora_fato'].max()} | "
                f"dist: {sub['target'].value_counts().sort_index().to_dict()}"
            )

    tr, va, te = parts["Treino"], parts["Validacao"], parts["Teste"]
    return SplitTemporal(
        X_train=tr[feature_cols], X_val=va[feature_cols], X_test=te[feature_cols],
        y_train=tr["target"].astype(int), y_val=va["target"].astype(int), y_test=te["target"].astype(int),
        ids_train=tr["fato_id"].reset_index(drop=True),
        ids_val=va["fato_id"].reset_index(drop=True),
        ids_test=te["fato_id"].reset_index(drop=True),
        feature_cols=feature_cols,
    )


# =====================================================================
# ETAPA 5 — TREINO (Gradient Boosting do sklearn)
# =====================================================================
# Substitui o XGBoost original por sklearn.GradientBoostingClassifier para
# evitar a dependencia C nativa libomp (macOS). Mesma familia de modelos
# (gradient boosting de arvores), API similar, e compativel com SHAP.


def treinar_modelo(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    **kwargs: Any,
) -> Tuple[GradientBoostingClassifier, Dict[int, int]]:
    """
    Treina GradientBoostingClassifier multiclasse com sample_weight balanceado.

    Mapeamos {-1,0,+1} -> {0,1,2} pra padronizar com codigos que assumem labels >= 0
    (e manter compatibilidade com a logica de avaliacao/SHAP).

    Notas vs XGBoost original:
        - Sem early stopping com eval_set custom (sklearn so suporta validacao
          interna aleatoria, o que violaria o split temporal). Compensamos com
          n_estimators=200 (menos que 500 do XGBoost) e learning_rate=0.05.
        - colsample_bytree do XGBoost vira max_features no sklearn.

    Args:
        X_train, y_train: dados de treino.
        X_val, y_val: dados de validacao — usados apenas pra reportar score de
                      monitoramento no log (nao para early stopping).
        **kwargs: override de hiperparametros (max_depth, learning_rate, ...).

    Returns:
        modelo treinado, inv_label_map (label_sklearn -> label_original em {-1,0,+1}).
    """
    log("ETAPA 5 - Treinando GradientBoostingClassifier (sklearn)...")

    label_map = {-1: 0, 0: 1, 1: 2}
    inv_label_map = {v: k for k, v in label_map.items()}

    y_tr = y_train.map(label_map).values
    y_va = y_val.map(label_map).values

    classes_presentes = np.unique(y_tr)
    pesos = compute_class_weight(class_weight="balanced", classes=classes_presentes, y=y_tr)
    peso_por_classe = {int(c): float(p) for c, p in zip(classes_presentes, pesos)}
    sample_weight = np.array([peso_por_classe.get(int(y), 1.0) for y in y_tr])

    params: Dict[str, Any] = dict(
        max_depth=5,
        learning_rate=0.05,
        n_estimators=200,
        subsample=0.8,
        max_features=0.8,
        random_state=RANDOM_STATE,
    )
    params.update(kwargs)

    modelo = GradientBoostingClassifier(**params)
    modelo.fit(X_train, y_tr, sample_weight=sample_weight)

    val_acc = float(modelo.score(X_val, y_va))
    log(f"ETAPA 5 - Treino concluido. n_estimators={params['n_estimators']}, val_acc(info)={val_acc:.4f}")
    return modelo, inv_label_map


# =====================================================================
# ETAPA 6 — AVALIACAO
# =====================================================================


def avaliar_modelo(
    modelo: GradientBoostingClassifier,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    inv_label_map: Dict[int, int],
) -> Dict[str, Any]:
    """
    Metricas no teste: Macro F1 (principal), log loss, Brier (one-vs-rest medio),
    matriz de confusao 3x3, classification report. Sem acuracia (classes desbalanceadas).
    """
    log("ETAPA 6 - Avaliando no conjunto de teste...")
    label_map = {v: k for k, v in inv_label_map.items()}
    y_te = y_test.map(label_map).values

    y_proba = modelo.predict_proba(X_test)
    y_pred = modelo.predict(X_test)

    nomes = {-1: "queda", 0: "neutro", 1: "alta"}
    target_names = [nomes[inv_label_map[i]] for i in range(3)]

    macro_f1 = float(f1_score(y_te, y_pred, average="macro"))
    cm = confusion_matrix(y_te, y_pred, labels=[0, 1, 2])
    ll = float(log_loss(y_te, y_proba, labels=[0, 1, 2]))
    brier = float(np.mean([
        brier_score_loss((y_te == i).astype(int), y_proba[:, i]) for i in range(3)
    ]))
    report = classification_report(
        y_te, y_pred, labels=[0, 1, 2], target_names=target_names, zero_division=0
    )

    log(f"  Macro F1: {macro_f1:.4f}  (meta: 0.45-0.55)")
    log(f"  Log Loss: {ll:.4f}")
    log(f"  Brier (media one-vs-rest): {brier:.4f}")
    log("  Matriz de Confusao (linhas=real, colunas=previsto):")
    log(f"    labels = {target_names}")
    for nome, linha in zip(target_names, cm):
        log(f"    {nome:7s} {linha.tolist()}")
    print(report)

    return {
        "macro_f1": macro_f1,
        "log_loss": ll,
        "brier": brier,
        "confusion_matrix": cm,
        "classification_report": report,
        "y_pred": y_pred,
        "y_proba": y_proba,
        "target_names": target_names,
    }


def sanity_check_sentimento(
    df_fatos: pd.DataFrame,
    ids_test: pd.Series,
    y_proba: np.ndarray,
    inv_label_map: Dict[int, int],
) -> Dict[str, Any]:
    """
    Cruza P(queda) prevista com sentimento do fato. Se corr ~ 1.0 -> o modelo
    apenas espelha o sentimento e features fundamentalistas nao agregam sinal.
    """
    log("ETAPA 6 - Sanity check: previsao vs sentimento...")
    idx_queda = {v: k for k, v in inv_label_map.items()}[-1]
    p_queda = y_proba[:, idx_queda]

    df_test = pd.DataFrame({"fato_id": ids_test.values, "p_queda": p_queda})
    df_test = df_test.merge(df_fatos[["fato_id", "sentimento"]], on="fato_id", how="left")
    df_test["sent_ord"] = df_test["sentimento"].map(SENTIMENTO_MAP)

    media_por_sent = df_test.groupby("sentimento")["p_queda"].mean().round(3).to_dict()
    corr = float(df_test[["sent_ord", "p_queda"]].corr().iloc[0, 1])

    log(f"  P(queda) media por sentimento: {media_por_sent}")
    log(f"  Correlacao(P(queda), sentimento_ord) = {corr:.3f}")
    if abs(corr) > 0.95:
        log("  ATENCAO: corr > 0.95 — o modelo pode estar apenas copiando o sentimento.")
    return {"correlacao": corr, "media_por_sentimento": media_por_sent}


# =====================================================================
# ETAPA 7 — INTERPRETABILIDADE
# =====================================================================


def plotar_feature_importance(
    modelo: GradientBoostingClassifier, top_n: int = 20, mostrar: bool = True
) -> pd.Series:
    """Grafico de barras das top_n features por gain do XGBoost."""
    importances = pd.Series(
        modelo.feature_importances_, index=modelo.feature_names_in_
    ).sort_values(ascending=False)

    fig, ax = plt.subplots(figsize=(8, max(4, top_n * 0.3)))
    importances.head(top_n).iloc[::-1].plot.barh(ax=ax, color="steelblue")
    ax.set_title("Feature Importance (XGBoost gain)")
    ax.set_xlabel("Importancia")
    plt.tight_layout()
    if mostrar:
        plt.show()
    return importances


def _shap_values_array(shap_vals: Any) -> np.ndarray:
    """Normaliza saida do shap em array (n_amostras, n_features, n_classes)."""
    if isinstance(shap_vals, list):
        # Lista por classe (formato classico) -> empilha em ultima dimensao
        return np.stack(shap_vals, axis=-1)
    arr = np.asarray(shap_vals)
    if arr.ndim == 3:
        return arr
    raise ValueError(f"Formato SHAP inesperado: shape={arr.shape}")


def _calcular_shap_values(
    modelo: GradientBoostingClassifier,
    X: pd.DataFrame,
    background: Optional[pd.DataFrame] = None,
) -> np.ndarray:
    """
    Calcula SHAP values com fallback automatico.

    Tenta TreeExplainer primeiro (rapido). Se nao suportar (caso do
    sklearn.GradientBoostingClassifier multiclasse), cai para o Explainer
    generico baseado em predict_proba (mais lento, mas funciona).
    """
    try:
        explainer = shap.TreeExplainer(modelo)
        return _shap_values_array(explainer.shap_values(X))
    except Exception as e:
        log(f"  TreeExplainer nao suporta esse modelo ({type(e).__name__}); "
            f"usando shap.Explainer generico (predict_proba)...")
        bg = background if background is not None else X.iloc[:min(20, len(X))]
        explainer = shap.Explainer(modelo.predict_proba, bg)
        explanation = explainer(X)
        arr = np.asarray(explanation.values)
        if arr.ndim == 3:
            return arr
        return _shap_values_array(arr)


def analise_shap(
    modelo: GradientBoostingClassifier,
    X_test: pd.DataFrame,
    y_pred: np.ndarray,
    inv_label_map: Dict[int, int],
    mostrar: bool = True,
) -> Dict[str, Any]:
    """Summary plot + force plot para 1 exemplo de cada classe prevista."""
    log("ETAPA 7 - Calculando SHAP values...")
    arr = _calcular_shap_values(modelo, X_test)

    # Summary global (importancia media absoluta, agregando todas as classes)
    plt.figure()
    shap.summary_plot(
        [arr[:, :, c] for c in range(arr.shape[-1])],
        X_test, plot_type="bar", show=False,
    )
    plt.title("SHAP - importancia media absoluta")
    plt.tight_layout()
    if mostrar:
        plt.show()

    exemplos: Dict[str, Dict[str, Any]] = {}
    nomes = {-1: "queda", 0: "neutro", 1: "alta"}
    for cls_xgb in range(arr.shape[-1]):
        cls_real = inv_label_map[cls_xgb]
        nome = nomes[cls_real]
        idxs = np.where(y_pred == cls_xgb)[0]
        if len(idxs) == 0:
            continue
        i = int(idxs[0])
        # Top 5 features que contribuem pra essa previsao
        sv = arr[i, :, cls_xgb]
        top5_idx = np.argsort(np.abs(sv))[::-1][:5]
        top5 = [(X_test.columns[j], float(sv[j])) for j in top5_idx]
        exemplos[nome] = {"idx_test": i, "top5": top5}
        log(f"  Exemplo previsto '{nome}' (idx {i}) - top 5 features:")
        for f, v in top5:
            log(f"    {f:35s} shap={v:+.4f}")
    return {"shap_values": arr, "exemplos": exemplos}


# =====================================================================
# ETAPA 8 — INFERENCIA EM FATO NOVO
# =====================================================================


@dataclass
class ArtefatosModelo:
    """Bundle persistivel com tudo que prever_reacao precisa."""
    modelo: GradientBoostingClassifier
    inv_label_map: Dict[int, int]
    feature_cols: List[str]
    setores_referencia: List[str]
    df_fatos_hist: pd.DataFrame
    df_ar_hist: pd.DataFrame
    df_fundamentalista: pd.DataFrame
    df_peers: pd.DataFrame


def prever_reacao(
    fato_dict: Dict[str, Any],
    df_cotacoes: pd.DataFrame,
    artefatos: ArtefatosModelo,
) -> Dict[str, Any]:
    """
    Inferencia para um fato novo.

    Args:
        fato_dict: {"ticker", "datahora_fato", "categoria", "sentimento",
                    "intensidade", "setor"} (e opcionalmente "fato_id").
        df_cotacoes: cotacoes horarias atualizadas (inclui IBOV).
        artefatos: bundle gerado por pipeline_completo / carregar_modelo.

    Returns:
        {"P_alta", "P_neutro", "P_queda", "top5_features_locais"}.
    """
    fato_id_novo = fato_dict.get("fato_id", "novo_fato")
    dt = pd.Timestamp(fato_dict["datahora_fato"])

    # Sanity check: garante que existe historia de cotacoes para o ticker
    if not ((df_cotacoes["ticker"] == fato_dict["ticker"]) & (df_cotacoes["datahora"] < dt)).any():
        warnings.warn(
            f"Sem cotacoes anteriores a {dt} para {fato_dict['ticker']} — "
            "features dependentes de historico de mercado podem ficar zeradas."
        )

    novo = pd.DataFrame([{
        "fato_id": fato_id_novo,
        "ticker": fato_dict["ticker"],
        "datahora_fato": dt,
        "categoria": fato_dict["categoria"],
        "sentimento": fato_dict["sentimento"],
        "intensidade": fato_dict["intensidade"],
        "setor": fato_dict["setor"],
    }])
    df_fatos_aug = pd.concat([artefatos.df_fatos_hist, novo], ignore_index=True)

    # AR do fato novo eh desconhecido — preenchemos com NaN; nao afeta features
    # historicas porque o filtro eh `<` em datahora.
    cols_ar = [c for c in artefatos.df_ar_hist.columns if c != "fato_id"]
    df_ar_aug = pd.concat([
        artefatos.df_ar_hist,
        pd.DataFrame([{"fato_id": fato_id_novo, **{c: np.nan for c in cols_ar}}]),
    ], ignore_index=True)

    df_feat_all = construir_features(
        df_fatos_aug, df_ar_aug, artefatos.df_fundamentalista, artefatos.df_peers,
        setores_referencia=artefatos.setores_referencia,
    )
    linha = df_feat_all[df_feat_all["fato_id"] == fato_id_novo].copy()

    # Alinha colunas com as do treino (preenche faltantes com 0, descarta extras)
    for c in artefatos.feature_cols:
        if c not in linha.columns:
            linha[c] = 0
    linha_X = linha[artefatos.feature_cols]

    proba = artefatos.modelo.predict_proba(linha_X)[0]
    idx = {v: k for k, v in artefatos.inv_label_map.items()}
    resultado = {
        "P_alta": float(proba[idx[1]]),
        "P_neutro": float(proba[idx[0]]),
        "P_queda": float(proba[idx[-1]]),
    }

    # SHAP local pra classe prevista
    cls_prevista = int(np.argmax(proba))
    arr = _calcular_shap_values(artefatos.modelo, linha_X)
    sv = arr[0, :, cls_prevista]
    top5_idx = np.argsort(np.abs(sv))[::-1][:5]
    resultado["top5_features_locais"] = [
        (artefatos.feature_cols[i], float(sv[i])) for i in top5_idx
    ]
    return resultado


# =====================================================================
# ORQUESTRADOR + PERSISTENCIA
# =====================================================================


def pipeline_completo(
    df_fatos: pd.DataFrame,
    df_cotacoes: pd.DataFrame,
    df_fundamentalista: pd.DataFrame,
    df_peers: pd.DataFrame,
    mostrar_plots: bool = True,
) -> Tuple[ArtefatosModelo, Dict[str, Any]]:
    """
    Executa ETAPAS 1-7 e devolve (artefatos para inferencia, dict de metricas).
    """
    log("=" * 70)
    log("PIPELINE B3-INSIGHT - INICIO")
    log("=" * 70)

    df_fatos, df_cotacoes, df_fundamentalista, df_peers = _normalizar_dataframes(
        df_fatos, df_cotacoes, df_fundamentalista, df_peers
    )

    df_ar = calcular_ar_combinado(df_cotacoes, df_fatos)

    # Validacao do AR — confirma que o sinal eh real antes de seguir o pipeline
    validacao_ar = validar_ar_placebo(df_cotacoes, df_fatos, df_ar)
    df_event_study = plotar_event_study(df_cotacoes, df_fatos, mostrar=mostrar_plots)

    df_fatos_t = calcular_threshold_dinamico(df_ar, df_fatos)
    df_fatos_t = definir_target(df_fatos_t)

    setores_ref = sorted(df_fatos["setor"].unique().tolist())
    df_features = construir_features(df_fatos_t, df_ar, df_fundamentalista, df_peers,
                                     setores_referencia=setores_ref)

    split = split_temporal(df_features, df_fatos_t)
    modelo, inv_label_map = treinar_modelo(split.X_train, split.y_train, split.X_val, split.y_val)
    metricas = avaliar_modelo(modelo, split.X_test, split.y_test, inv_label_map)
    metricas["sanity_check"] = sanity_check_sentimento(
        df_fatos_t, split.ids_test, metricas["y_proba"], inv_label_map
    )

    plotar_feature_importance(modelo, mostrar=mostrar_plots)
    metricas["shap"] = analise_shap(
        modelo, split.X_test, metricas["y_pred"], inv_label_map, mostrar=mostrar_plots
    )
    metricas["validacao_ar"] = validacao_ar
    metricas["event_study"] = df_event_study

    artefatos = ArtefatosModelo(
        modelo=modelo,
        inv_label_map=inv_label_map,
        feature_cols=split.feature_cols,
        setores_referencia=setores_ref,
        df_fatos_hist=df_fatos_t,
        df_ar_hist=df_ar,
        df_fundamentalista=df_fundamentalista,
        df_peers=df_peers,
    )

    log("=" * 70)
    log("PIPELINE - CONCLUIDO")
    log("=" * 70)
    return artefatos, metricas


def salvar_modelo(artefatos: ArtefatosModelo, caminho: str = "modelo_b3insight.joblib") -> None:
    """Persiste o bundle inteiro via joblib."""
    joblib.dump(artefatos, caminho)
    log(f"Modelo salvo em: {caminho}")


def carregar_modelo(caminho: str = "modelo_b3insight.joblib") -> ArtefatosModelo:
    """Carrega bundle salvo via joblib."""
    artefatos: ArtefatosModelo = joblib.load(caminho)
    log(f"Modelo carregado de: {caminho}")
    return artefatos
