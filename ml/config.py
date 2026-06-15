"""
ml/config.py
============
Hiperparâmetros compartilhados da cascata, num único lugar — usados tanto pelo
re-treino de produção (pipeline._treinar) quanto pela geração de previsões
HONESTAS da demonstração (artefatos), garantindo que o modelo de holdout da tela
/prever use exatamente a mesma configuração do modelo reportado nas métricas.
"""
from __future__ import annotations

# fração temporal usada para treino (resto é holdout — nunca visto)
FRACAO_TREINO = 0.85

# Estágio 1 — porteiro binário "vai mexer?" (regularizado, honesto)
P1_PORTEIRO = dict(
    n_estimators=250, max_depth=2, learning_rate=0.03, subsample=0.7,
    colsample_bytree=0.6, min_child_weight=30, reg_lambda=8.0,
)

# Estágio 2 — direção alta/queda (experimental, baixa confiança)
P2_DIRECAO = dict(
    n_estimators=200, max_depth=2, learning_rate=0.03,
    min_child_weight=15, reg_lambda=6.0,
)
