# 📊 Radar B3 — Análise Fundamentalista de Ações com IA

> 🌐 [**Português**](#-português) · [**English**](#-english) · [**Italiano**](#-italiano)

<p align="left">
  <img src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13">
  <img src="https://img.shields.io/badge/Flask-3.x-000000?logo=flask&logoColor=white" alt="Flask">
  <img src="https://img.shields.io/badge/XGBoost-2.x-EB5E28" alt="XGBoost">
  <img src="https://img.shields.io/badge/AWS-S3%20%C2%B7%20ECS%20Fargate%20%C2%B7%20ECR-FF9900?logo=amazonaws&logoColor=white" alt="AWS">
  <img src="https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/package%20manager-uv-DE5FE9" alt="uv">
</p>

---

# 🇧🇷 Português

> Plataforma que coleta, processa e analisa dados de empresas listadas na **B3**
> (bolsa brasileira) e entrega ao investidor leigo uma leitura simples da saúde
> financeira de cada ação — fatos relevantes classificados, preço histórico,
> score fundamentalista e um **modelo de Machine Learning** que estima a reação
> do preço a novos fatos relevantes.

Trabalho da disciplina de **Cloud Computing & Big Data**.

## 📑 Índice

- [O que o projeto faz](#-o-que-o-projeto-faz)
- [Arquitetura](#️-arquitetura)
- [O modelo de Machine Learning](#-o-modelo-de-machine-learning)
- [Estrutura do repositório](#-estrutura-do-repositório)
- [Stack](#-stack)
- [Rodando localmente](#-rodando-localmente)
- [Variáveis de ambiente](#-variáveis-de-ambiente)
- [Deploy na AWS](#-deploy-na-aws)
- [Rotas do dashboard](#-rotas-do-dashboard)
- [Segurança](#-segurança)
- [Autores](#-autores)

## 🎯 O que o projeto faz

O usuário pesquisa uma empresa (ex.: *Petrobras*) e recebe, em linguagem simples:

- 📈 **Preço histórico** e cotação (Yahoo Finance)
- 📰 **Fatos relevantes** publicados oficialmente na B3, já classificados por
  sinal (positivo/negativo) e tipo (resultado, dividendo, aquisição, etc.)
- 🧮 **Indicadores fundamentalistas** e um score de saúde financeira
- 🤖 **Previsão de ML**: dado um novo fato relevante, o modelo estima se o preço
  tende a se mover de forma anormal e em que direção

O **escopo do protótipo** são **20 empresas** de 9 setores, com até **10 anos**
de histórico (2015–2025):

`PETR4` `VALE3` `ITUB4` `BBDC4` `BBAS3` `ABEV3` `MGLU3` `WEGE3` `EMBR3` `JBSS3`
`SUZB3` `RENT3` `TOTS3` `LREN3` `ELET3` `CSAN3` `RAIL3` `RDOR3` `HAPV3` `BRFS3`

## 🏗️ Arquitetura

Pipeline de dados no padrão **Medallion** (bronze → silver → gold) sobre S3,
com ingestão contínua e apresentação em Flask. O mesmo container roda em **EC2**
e em **ECS/Fargate**, demonstrando a migração de compute.

```
   FONTES                INGESTÃO              DATA LAKE (S3)            APRESENTAÇÃO
┌────────────┐        ┌─────────────┐      ┌──────────────────┐      ┌──────────────┐
│ B3 (API)   │        │  worker.py  │      │  bronze/  (raw)  │      │ Flask :5050  │
│ CVM        │ ─────▶ │  (systemd)  │ ───▶ │  silver/  (limpo)│ ───▶ │  dashboard + │
│ Yahoo Fin. │  1h/1d │  schedule   │      │  gold/    (joins)│      │  telas de ML │
│ Fundamentus│        └─────────────┘      └──────────────────┘      └──────┬───────┘
└────────────┘               │                      ▲                       │
                             │  rotulagem IA        │  artefatos ML         ▼
                             ▼  (gpt-4o-mini)       │                  CloudFront (HTTPS)
                      ┌─────────────┐               │                       │
                      │  modelo ML  │ ──────────────┘                  ALB :80/:443
                      │  (XGBoost)  │                                       │
                      └─────────────┘                              ECS Fargate / EC2
```

**Fluxo:**

1. **`scripts/worker.py`** (serviço `systemd`) coleta de hora em hora cotações
   (Yahoo) e fatos relevantes (B3/CVM), e 1×/dia os fundamentos (Fundamentus).
2. O scraper grava em **bronze**; `pipeline/silver_transform.py` limpa para
   **silver**; `pipeline/gold_transform.py` cruza fatos × preços para **gold**.
3. O **modelo de ML** consome a camada gold, rotula PDFs de fatos com IA e
   retreina semanalmente (*self-feeding*).
4. O **dashboard Flask** lê do S3 (via IAM Role — sem chaves no código) e serve
   as páginas e a API. Em produção fica atrás de **ALB → CloudFront** (HTTPS).

> Toda comunicação com o S3 passa por `storage/s3_manager` — nenhum outro módulo
> chama `boto3` diretamente.

## 🧠 O modelo de Machine Learning

Modelo em **cascata** (dois estágios XGBoost) que estima a reação do preço a um
fato relevante, treinado com avaliação **honesta** (holdout temporal — o conjunto
de teste é sempre o período mais recente, nunca visto no treino).

| Estágio | Pergunta | Saída |
|---|---|---|
| **1 — Porteiro** | "Esse fato vai mexer no preço?" | binário (mexe / não mexe), regularizado |
| **2 — Direção** | "Se mexer, é alta ou queda?" | direção (experimental, baixa confiança) |

- **Rótulos**: gerados a partir do retorno anormal (*event study* sobre os preços)
  e enriquecidos por **IA** — `ml/rotular.py` lê o texto dos PDFs e classifica o
  fato via OpenAI **gpt-4o-mini**.
- **Self-feeding**: `worker.py` re-rotula e **retreina semanalmente**; as telas
  do dashboard são atualizadas de hora em hora.
- **Modelo-semente**: `ml/artefatos_seed/modelo_final.joblib` vai embarcado na
  imagem Docker, garantindo que as telas `/treino` e `/prever` funcionem já no
  primeiro boot, antes do primeiro retreino em produção.
- **Configuração única**: hiperparâmetros centralizados em `ml/config.py`, usados
  tanto pelo retreino de produção quanto pelas previsões da demonstração.

> ⚠️ É um protótipo acadêmico. O estágio 2 (direção) tem baixa confiança e está
> documentado como experimental — **não é recomendação de investimento.**

## 📂 Estrutura do repositório

```
.
├── scraper/              # Coleta de fatos relevantes via API REST da B3 (requests)
│   ├── scraper.py
│   └── config.py         # as 20 empresas, período e prefixos Medallion
├── pipeline/             # Transformações do data lake
│   ├── silver_transform.py   # bronze (raw) → silver (parquet limpo)
│   └── gold_transform.py     # silver → gold (fatos × preços, resumo por empresa)
├── ml/                   # Machine Learning (cascata XGBoost + rotulagem IA)
│   ├── engine.py         # event study / retorno anormal (CAPM; ARIMA opcional)
│   ├── pipeline.py       # orquestra dataset → treino → métricas
│   ├── rotular.py        # rotulagem dos PDFs via OpenAI gpt-4o-mini
│   ├── scoring.py        # score fundamentalista
│   ├── artefatos.py      # geração dos artefatos honestos da demonstração
│   ├── loader.py         # leitura dos dados (gold / S3)
│   ├── config.py         # hiperparâmetros da cascata
│   └── artefatos_seed/   # modelo-semente embarcado no Docker (versionado)
├── dashboard/            # Aplicação web
│   ├── server.py         # Flask, porta 5050 (rotas + API JSON)
│   └── static/           # front-end
├── storage/
│   └── s3_manager/       # única camada que fala com o S3 (boto3)
├── scripts/              # Operação e ingestão
│   ├── worker.py         # agendador (systemd) — coleta + retreino
│   ├── market_data.py    # cotações (Yahoo Finance)
│   ├── fundamentalista.py# fundamentos (Fundamentus)
│   ├── download_pdfs_faltantes.py
│   ├── backfill_historico.py
│   ├── upload_s3.py
│   └── validar_pipeline.py
├── infra/                # Infra (EC2)
│   ├── setup_ec2.sh      # provisionamento do ambiente
│   ├── radar-b3.service  # unit do systemd
│   └── s3_init.py        # cria o bucket / estrutura de prefixos
├── tests/                # Testes automatizados
├── docs/                 # Documentação
├── Dockerfile            # imagem Python 3.13-slim (uv sync)
├── buildspec.yml         # build no AWS CodeBuild → push ECR
├── DEPLOY.md             # runbook de deploy AWS (recursos, comandos, teardown)
├── PLANO-INFRA.md        # passo a passo ALB + ACM + CloudFront
├── pyproject.toml        # dependências (uv)
└── uv.lock               # lockfile (commitado)
```

## 🛠 Stack

| Camada | Tecnologias |
|---|---|
| **Linguagem** | Python 3.13, gerenciado com [`uv`](https://github.com/astral-sh/uv) |
| **Coleta** | `requests` (API REST da B3), `yfinance`, Fundamentus |
| **Processamento** | `pandas`, `numpy`, `pyarrow` (Parquet), `pymupdf` (texto de PDF) |
| **Machine Learning** | `scikit-learn`, `xgboost`, `joblib`; OpenAI `gpt-4o-mini` (rotulagem) |
| **Web** | `flask`, `flask-cors`, `plotly` |
| **Cloud** | AWS S3, EC2, ECS Fargate, ECR, CloudFront, ALB, IAM, CloudWatch |
| **Container / CI** | Docker, AWS CodeBuild (`buildspec.yml`) |
| **Agendamento** | `schedule` + `systemd` |

## 💻 Rodando localmente

> Pré-requisitos: **Python 3.13** e [**uv**](https://github.com/astral-sh/uv)
> (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
# 1. Instalar dependências (cria a .venv e resolve o lock)
uv sync

# 2. Configurar o ambiente
cp .env.example .env        # ajuste o bucket / região se necessário
# para usar a rotulagem por IA, exporte sua chave da OpenAI:
export OPENAI_API_KEY="sk-..."

# 3. (Opcional) Coletar dados para a máquina local
uv run python scripts/market_data.py      # cotações
uv run python scraper/scraper.py          # fatos relevantes da B3

# 4. Processar o data lake (bronze → silver → gold)
uv run python pipeline/silver_transform.py
uv run python pipeline/gold_transform.py

# 5. Subir o dashboard
uv run python dashboard/server.py
# → http://localhost:5050
```

Para rodar a ingestão contínua (worker) em um segundo terminal:

```bash
uv run python scripts/worker.py
```

### Via Docker

```bash
docker build -t b3insight .
docker run -p 5050:5050 --env-file .env b3insight
# → http://localhost:5050
```

## 🔑 Variáveis de ambiente

Copie `.env.example` para `.env`. **Nunca** coloque chaves no repositório.

| Variável | Descrição | Padrão |
|---|---|---|
| `S3_BUCKET_NAME` | Bucket do data lake | `b3insight-data` |
| `AWS_DEFAULT_REGION` | Região AWS | `us-east-1` |
| `FLASK_PORT` | Porta do dashboard | `5050` |
| `OPENAI_API_KEY` | Chave da OpenAI (rotulagem de PDFs) | — *(obrigatória só para a IA)* |

> Na AWS, as credenciais do S3 vêm do **IAM Role** da EC2/ECS — não há chaves AWS
> no código nem no `.env`. A chave da OpenAI é a única credencial de aplicação.

## ☁️ Deploy na AWS

O fluxo completo (recursos provisionados, comandos de operação e **teardown**)
está em **[`DEPLOY.md`](./DEPLOY.md)**. O passo a passo do ALB + ACM + CloudFront
está em **[`PLANO-INFRA.md`](./PLANO-INFRA.md)**.

Resumo:

```
git push ──▶ CodeBuild (buildspec.yml) ──▶ ECR ──▶ ECS Fargate
                                                       │
                                              ALB :80/:443
                                                       │
                                           CloudFront (HTTPS público)
```

- **Storage**: bucket S3 em camadas `bronze/ silver/ gold/`.
- **Ingestão**: `worker.py` como serviço `systemd` na EC2.
- **Apresentação**: dashboard Flask roda na EC2 **e** no ECS/Fargate, ambos
  lendo do S3 via IAM Role.

> 💸 Lembre-se de rodar o **teardown** do `DEPLOY.md` após a entrega para não
> gerar cobrança (EC2, Elastic IP, ECS, ALB, CloudFront).

## 🌐 Rotas do dashboard

| Rota | Tipo | Descrição |
|---|---|---|
| `/` | página | Busca de empresa + visão geral |
| `/modelo` | página | Resultados e métricas do modelo de ML |
| `/treino` | página | Visualização do treino |
| `/prever` | página | Previsão para um fato relevante |
| `/status` | health | Health check (HTTP 200) — usado pelo ALB |
| `/api/empresas` | JSON | Lista das empresas |
| `/api/precos/<ticker>` | JSON | Série de preços |
| `/api/fatos/<codigo_cvm>` | JSON | Fatos relevantes da empresa |
| `/api/modelo`, `/api/modelo/<cvm>` | JSON | Saída do modelo |
| `/api/treino`, `/api/prever`, `/api/prever/sortear` | JSON | Telas de ML |

## 🔒 Segurança

- Segredos (`.env*`, `*.pem`, chaves) estão cobertos pelo `.gitignore` — confira
  antes de qualquer `git push`.
- As credenciais AWS são providas por **IAM Role**, não por chaves no código.
- O Security Group de demonstração abre as portas `22` e `5050` para `0.0.0.0/0`;
  em produção, restrinja por IP de origem.
- Antes de tornar o repositório público, **rotacione** qualquer chave que já tenha
  existido localmente (OpenAI, key pair EC2).

## 📜 Licença

**Todos os direitos reservados** — © 2026 Pedro Miranda e Luigi Ajello.

Este é um trabalho acadêmico de código *visível, mas não reutilizável*: você pode
**ler e avaliar** o código, mas **não** pode usá-lo, copiá-lo, modificá-lo ou
redistribuí-lo sem autorização por escrito dos autores. Veja os termos completos
em [`LICENSE`](./LICENSE).

Os dados acessados pertencem às suas fontes (B3, CVM, Yahoo Finance, Fundamentus)
e seguem os termos de uso de cada provedor. Projeto sem fins comerciais.

## 👥 Autores

- **Pedro Miranda**
- **Luigi Ajello**

Disciplina: *Cloud Computing & Big Data*.

<br>

---

<br>

# 🇬🇧 English

> A platform that collects, processes and analyzes data from companies listed on
> the **B3** (Brazilian stock exchange) and gives the everyday investor a simple
> reading of each stock's financial health — classified material facts, price
> history, a fundamental score and a **Machine Learning model** that estimates the
> price reaction to new material facts.

Project for the **Cloud Computing & Big Data** course.

## 📑 Table of Contents

- [What the project does](#-what-the-project-does)
- [Architecture](#️-architecture)
- [The Machine Learning model](#-the-machine-learning-model)
- [Repository structure](#-repository-structure)
- [Stack](#-stack-1)
- [Running locally](#-running-locally)
- [Environment variables](#-environment-variables)
- [Deploying to AWS](#️-deploying-to-aws)
- [Dashboard routes](#-dashboard-routes)
- [Security](#-security)
- [License](#-license)
- [Authors](#-authors-1)

## 🎯 What the project does

The user searches for a company (e.g. *Petrobras*) and receives, in plain language:

- 📈 **Price history** and quotes (Yahoo Finance)
- 📰 **Material facts** officially published on the B3, already classified by
  signal (positive/negative) and type (earnings, dividend, acquisition, etc.)
- 🧮 **Fundamental indicators** and a financial health score
- 🤖 **ML prediction**: given a new material fact, the model estimates whether the
  price tends to move abnormally and in which direction

The **prototype scope** is **20 companies** across 9 sectors, with up to **10 years**
of history (2015–2025):

`PETR4` `VALE3` `ITUB4` `BBDC4` `BBAS3` `ABEV3` `MGLU3` `WEGE3` `EMBR3` `JBSS3`
`SUZB3` `RENT3` `TOTS3` `LREN3` `ELET3` `CSAN3` `RAIL3` `RDOR3` `HAPV3` `BRFS3`

## 🏗️ Architecture

Data pipeline following the **Medallion** pattern (bronze → silver → gold) on S3,
with continuous ingestion and a Flask presentation layer. The same container runs
on **EC2** and on **ECS/Fargate**, demonstrating the compute migration.

```
   SOURCES               INGESTION             DATA LAKE (S3)            PRESENTATION
┌────────────┐        ┌─────────────┐      ┌──────────────────┐      ┌──────────────┐
│ B3 (API)   │        │  worker.py  │      │  bronze/  (raw)  │      │ Flask :5050  │
│ CVM        │ ─────▶ │  (systemd)  │ ───▶ │  silver/  (clean)│ ───▶ │  dashboard + │
│ Yahoo Fin. │  1h/1d │  schedule   │      │  gold/    (joins)│      │  ML screens  │
│ Fundamentus│        └─────────────┘      └──────────────────┘      └──────┬───────┘
└────────────┘               │                      ▲                       │
                             │  AI labeling         │  ML artifacts         ▼
                             ▼  (gpt-4o-mini)       │                  CloudFront (HTTPS)
                      ┌─────────────┐               │                       │
                      │  ML model   │ ──────────────┘                  ALB :80/:443
                      │  (XGBoost)  │                                       │
                      └─────────────┘                              ECS Fargate / EC2
```

**Flow:**

1. **`scripts/worker.py`** (a `systemd` service) collects quotes hourly (Yahoo)
   and material facts (B3/CVM), and fundamentals once a day (Fundamentus).
2. The scraper writes to **bronze**; `pipeline/silver_transform.py` cleans it into
   **silver**; `pipeline/gold_transform.py` joins facts × prices into **gold**.
3. The **ML model** consumes the gold layer, labels material-fact PDFs with AI and
   retrains weekly (*self-feeding*).
4. The **Flask dashboard** reads from S3 (via IAM Role — no keys in code) and serves
   the pages and the API. In production it sits behind **ALB → CloudFront** (HTTPS).

> All communication with S3 goes through `storage/s3_manager` — no other module
> calls `boto3` directly.

## 🧠 The Machine Learning model

A **cascade** model (two XGBoost stages) that estimates the price reaction to a
material fact, trained with **honest** evaluation (temporal holdout — the test set
is always the most recent period, never seen during training).

| Stage | Question | Output |
|---|---|---|
| **1 — Gatekeeper** | "Will this fact move the price?" | binary (moves / doesn't), regularized |
| **2 — Direction** | "If it moves, up or down?" | direction (experimental, low confidence) |

- **Labels**: generated from the abnormal return (*event study* over prices) and
  enriched by **AI** — `ml/rotular.py` reads the PDF text and classifies the fact
  via OpenAI **gpt-4o-mini**.
- **Self-feeding**: `worker.py` re-labels and **retrains weekly**; the dashboard
  screens are refreshed every hour.
- **Seed model**: `ml/artefatos_seed/modelo_final.joblib` is embedded in the Docker
  image, ensuring the `/treino` and `/prever` screens work on the very first boot,
  before the first production retrain.
- **Single configuration**: hyperparameters are centralized in `ml/config.py`, used
  by both the production retrain and the demo predictions.

> ⚠️ This is an academic prototype. Stage 2 (direction) has low confidence and is
> documented as experimental — **it is not investment advice.**

## 📂 Repository structure

```
.
├── scraper/              # Collects material facts via the B3 REST API (requests)
│   ├── scraper.py
│   └── config.py         # the 20 companies, period and Medallion prefixes
├── pipeline/             # Data lake transformations
│   ├── silver_transform.py   # bronze (raw) → silver (clean parquet)
│   └── gold_transform.py     # silver → gold (facts × prices, per-company summary)
├── ml/                   # Machine Learning (XGBoost cascade + AI labeling)
│   ├── engine.py         # event study / abnormal return (CAPM; optional ARIMA)
│   ├── pipeline.py       # orchestrates dataset → training → metrics
│   ├── rotular.py        # PDF labeling via OpenAI gpt-4o-mini
│   ├── scoring.py        # fundamental score
│   ├── artefatos.py      # generation of the honest demo artifacts
│   ├── loader.py         # data loading (gold / S3)
│   ├── config.py         # cascade hyperparameters
│   └── artefatos_seed/   # seed model embedded in Docker (versioned)
├── dashboard/            # Web application
│   ├── server.py         # Flask, port 5050 (routes + JSON API)
│   └── static/           # front-end
├── storage/
│   └── s3_manager/       # the only layer that talks to S3 (boto3)
├── scripts/              # Operation and ingestion
│   ├── worker.py         # scheduler (systemd) — collection + retraining
│   ├── market_data.py    # quotes (Yahoo Finance)
│   ├── fundamentalista.py# fundamentals (Fundamentus)
│   ├── download_pdfs_faltantes.py
│   ├── backfill_historico.py
│   ├── upload_s3.py
│   └── validar_pipeline.py
├── infra/                # Infrastructure (EC2)
│   ├── setup_ec2.sh      # environment provisioning
│   ├── radar-b3.service  # systemd unit
│   └── s3_init.py        # creates the bucket / prefix structure
├── tests/                # Automated tests
├── docs/                 # Documentation
├── Dockerfile            # Python 3.13-slim image (uv sync)
├── buildspec.yml         # build on AWS CodeBuild → push to ECR
├── DEPLOY.md             # AWS deploy runbook (resources, commands, teardown)
├── PLANO-INFRA.md        # step-by-step ALB + ACM + CloudFront
├── pyproject.toml        # dependencies (uv)
└── uv.lock               # lockfile (committed)
```

## 🛠 Stack

| Layer | Technologies |
|---|---|
| **Language** | Python 3.13, managed with [`uv`](https://github.com/astral-sh/uv) |
| **Collection** | `requests` (B3 REST API), `yfinance`, Fundamentus |
| **Processing** | `pandas`, `numpy`, `pyarrow` (Parquet), `pymupdf` (PDF text) |
| **Machine Learning** | `scikit-learn`, `xgboost`, `joblib`; OpenAI `gpt-4o-mini` (labeling) |
| **Web** | `flask`, `flask-cors`, `plotly` |
| **Cloud** | AWS S3, EC2, ECS Fargate, ECR, CloudFront, ALB, IAM, CloudWatch |
| **Container / CI** | Docker, AWS CodeBuild (`buildspec.yml`) |
| **Scheduling** | `schedule` + `systemd` |

## 💻 Running locally

> Prerequisites: **Python 3.13** and [**uv**](https://github.com/astral-sh/uv)
> (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
# 1. Install dependencies (creates the .venv and resolves the lock)
uv sync

# 2. Configure the environment
cp .env.example .env        # adjust the bucket / region if needed
# to use AI labeling, export your OpenAI key:
export OPENAI_API_KEY="sk-..."

# 3. (Optional) Collect data for the local machine
uv run python scripts/market_data.py      # quotes
uv run python scraper/scraper.py          # B3 material facts

# 4. Process the data lake (bronze → silver → gold)
uv run python pipeline/silver_transform.py
uv run python pipeline/gold_transform.py

# 5. Start the dashboard
uv run python dashboard/server.py
# → http://localhost:5050
```

To run continuous ingestion (worker) in a second terminal:

```bash
uv run python scripts/worker.py
```

### Via Docker

```bash
docker build -t b3insight .
docker run -p 5050:5050 --env-file .env b3insight
# → http://localhost:5050
```

## 🔑 Environment variables

Copy `.env.example` to `.env`. **Never** put keys in the repository.

| Variable | Description | Default |
|---|---|---|
| `S3_BUCKET_NAME` | Data lake bucket | `b3insight-data` |
| `AWS_DEFAULT_REGION` | AWS region | `us-east-1` |
| `FLASK_PORT` | Dashboard port | `5050` |
| `OPENAI_API_KEY` | OpenAI key (PDF labeling) | — *(required only for AI)* |

> On AWS, S3 credentials come from the EC2/ECS **IAM Role** — there are no AWS keys
> in the code or in `.env`. The OpenAI key is the only application credential.

## ☁️ Deploying to AWS

The full flow (provisioned resources, operation commands and **teardown**) is in
**[`DEPLOY.md`](./DEPLOY.md)**. The step-by-step for ALB + ACM + CloudFront is in
**[`PLANO-INFRA.md`](./PLANO-INFRA.md)**.

Summary:

```
git push ──▶ CodeBuild (buildspec.yml) ──▶ ECR ──▶ ECS Fargate
                                                       │
                                              ALB :80/:443
                                                       │
                                           CloudFront (public HTTPS)
```

- **Storage**: S3 bucket in `bronze/ silver/ gold/` layers.
- **Ingestion**: `worker.py` as a `systemd` service on EC2.
- **Presentation**: the Flask dashboard runs on EC2 **and** on ECS/Fargate, both
  reading from S3 via IAM Role.

> 💸 Remember to run the **teardown** in `DEPLOY.md` after delivery to avoid charges
> (EC2, Elastic IP, ECS, ALB, CloudFront).

## 🌐 Dashboard routes

| Route | Type | Description |
|---|---|---|
| `/` | page | Company search + overview |
| `/modelo` | page | ML model results and metrics |
| `/treino` | page | Training visualization |
| `/prever` | page | Prediction for a material fact |
| `/status` | health | Health check (HTTP 200) — used by the ALB |
| `/api/empresas` | JSON | List of companies |
| `/api/precos/<ticker>` | JSON | Price series |
| `/api/fatos/<codigo_cvm>` | JSON | Company material facts |
| `/api/modelo`, `/api/modelo/<cvm>` | JSON | Model output |
| `/api/treino`, `/api/prever`, `/api/prever/sortear` | JSON | ML screens |

## 🔒 Security

- Secrets (`.env*`, `*.pem`, keys) are covered by `.gitignore` — double-check before
  any `git push`.
- AWS credentials are provided by **IAM Role**, not by keys in the code.
- The demo Security Group opens ports `22` and `5050` to `0.0.0.0/0`; in production,
  restrict by source IP.
- Before making the repository public, **rotate** any key that has ever existed
  locally (OpenAI, EC2 key pair).

## 📜 License

**All rights reserved** — © 2026 Pedro Miranda and Luigi Ajello.

This is an academic work with *visible but non-reusable* code: you may **read and
evaluate** the code, but you may **not** use, copy, modify or redistribute it
without written authorization from the authors. See the full terms in
[`LICENSE`](./LICENSE).

The data accessed belongs to its sources (B3, CVM, Yahoo Finance, Fundamentus) and
follows each provider's terms of use. Non-commercial project.

## 👥 Authors

- **Pedro Miranda**
- **Luigi Ajello**

Course: *Cloud Computing & Big Data*.

<br>

---

<br>

# 🇮🇹 Italiano

> Una piattaforma che raccoglie, elabora e analizza i dati delle società quotate
> alla **B3** (la borsa brasiliana) e offre all'investitore comune una lettura
> semplice della salute finanziaria di ciascun titolo — fatti rilevanti
> classificati, storico dei prezzi, un punteggio fondamentale e un **modello di
> Machine Learning** che stima la reazione del prezzo a nuovi fatti rilevanti.

Progetto per il corso di **Cloud Computing & Big Data**.

## 📑 Indice

- [Cosa fa il progetto](#-cosa-fa-il-progetto)
- [Architettura](#️-architettura)
- [Il modello di Machine Learning](#-il-modello-di-machine-learning)
- [Struttura del repository](#-struttura-del-repository)
- [Stack](#-stack-2)
- [Esecuzione in locale](#-esecuzione-in-locale)
- [Variabili d'ambiente](#-variabili-dambiente)
- [Deploy su AWS](#️-deploy-su-aws)
- [Rotte della dashboard](#-rotte-della-dashboard)
- [Sicurezza](#-sicurezza)
- [Licenza](#-licenza)
- [Autori](#-autori)

## 🎯 Cosa fa il progetto

L'utente cerca una società (es.: *Petrobras*) e riceve, in linguaggio semplice:

- 📈 **Storico dei prezzi** e quotazioni (Yahoo Finance)
- 📰 **Fatti rilevanti** pubblicati ufficialmente sulla B3, già classificati per
  segnale (positivo/negativo) e tipo (risultato, dividendo, acquisizione, ecc.)
- 🧮 **Indicatori fondamentali** e un punteggio di salute finanziaria
- 🤖 **Previsione ML**: dato un nuovo fatto rilevante, il modello stima se il
  prezzo tende a muoversi in modo anomalo e in quale direzione

L'**ambito del prototipo** comprende **20 società** di 9 settori, con fino a
**10 anni** di storico (2015–2025):

`PETR4` `VALE3` `ITUB4` `BBDC4` `BBAS3` `ABEV3` `MGLU3` `WEGE3` `EMBR3` `JBSS3`
`SUZB3` `RENT3` `TOTS3` `LREN3` `ELET3` `CSAN3` `RAIL3` `RDOR3` `HAPV3` `BRFS3`

## 🏗️ Architettura

Pipeline di dati secondo il pattern **Medallion** (bronze → silver → gold) su S3,
con ingestione continua e presentazione in Flask. Lo stesso container gira su
**EC2** e su **ECS/Fargate**, dimostrando la migrazione del compute.

```
   FONTI                 INGESTIONE            DATA LAKE (S3)            PRESENTAZIONE
┌────────────┐        ┌─────────────┐      ┌──────────────────┐      ┌──────────────┐
│ B3 (API)   │        │  worker.py  │      │  bronze/  (raw)  │      │ Flask :5050  │
│ CVM        │ ─────▶ │  (systemd)  │ ───▶ │  silver/ (pulito)│ ───▶ │  dashboard + │
│ Yahoo Fin. │  1h/1d │  schedule   │      │  gold/    (joins)│      │  schermate ML│
│ Fundamentus│        └─────────────┘      └──────────────────┘      └──────┬───────┘
└────────────┘               │                      ▲                       │
                             │  etichettatura IA    │  artefatti ML         ▼
                             ▼  (gpt-4o-mini)       │                  CloudFront (HTTPS)
                      ┌─────────────┐               │                       │
                      │  modello ML │ ──────────────┘                  ALB :80/:443
                      │  (XGBoost)  │                                       │
                      └─────────────┘                              ECS Fargate / EC2
```

**Flusso:**

1. **`scripts/worker.py`** (servizio `systemd`) raccoglie ogni ora le quotazioni
   (Yahoo) e i fatti rilevanti (B3/CVM), e una volta al giorno i fondamentali
   (Fundamentus).
2. Lo scraper scrive in **bronze**; `pipeline/silver_transform.py` pulisce in
   **silver**; `pipeline/gold_transform.py` incrocia fatti × prezzi in **gold**.
3. Il **modello ML** consuma il livello gold, etichetta i PDF dei fatti con l'IA e
   si riaddestra ogni settimana (*self-feeding*).
4. La **dashboard Flask** legge da S3 (tramite IAM Role — nessuna chiave nel
   codice) e serve le pagine e l'API. In produzione sta dietro a
   **ALB → CloudFront** (HTTPS).

> Tutta la comunicazione con S3 passa da `storage/s3_manager` — nessun altro modulo
> chiama `boto3` direttamente.

## 🧠 Il modello di Machine Learning

Modello a **cascata** (due stadi XGBoost) che stima la reazione del prezzo a un
fatto rilevante, addestrato con una valutazione **onesta** (holdout temporale — il
set di test è sempre il periodo più recente, mai visto in addestramento).

| Stadio | Domanda | Output |
|---|---|---|
| **1 — Portiere** | "Questo fatto muoverà il prezzo?" | binario (muove / non muove), regolarizzato |
| **2 — Direzione** | "Se muove, sale o scende?" | direzione (sperimentale, bassa affidabilità) |

- **Etichette**: generate dal rendimento anomalo (*event study* sui prezzi) e
  arricchite dall'**IA** — `ml/rotular.py` legge il testo dei PDF e classifica il
  fatto tramite OpenAI **gpt-4o-mini**.
- **Self-feeding**: `worker.py` rietichetta e **si riaddestra ogni settimana**; le
  schermate della dashboard vengono aggiornate ogni ora.
- **Modello-seme**: `ml/artefatos_seed/modelo_final.joblib` è incorporato
  nell'immagine Docker, garantendo che le schermate `/treino` e `/prever` funzionino
  già al primo avvio, prima del primo riaddestramento in produzione.
- **Configurazione unica**: gli iperparametri sono centralizzati in `ml/config.py`,
  usati sia dal riaddestramento di produzione sia dalle previsioni della demo.

> ⚠️ È un prototipo accademico. Lo stadio 2 (direzione) ha bassa affidabilità ed è
> documentato come sperimentale — **non è una raccomandazione d'investimento.**

## 📂 Struttura del repository

```
.
├── scraper/              # Raccolta dei fatti rilevanti tramite API REST della B3 (requests)
│   ├── scraper.py
│   └── config.py         # le 20 società, il periodo e i prefissi Medallion
├── pipeline/             # Trasformazioni del data lake
│   ├── silver_transform.py   # bronze (raw) → silver (parquet pulito)
│   └── gold_transform.py     # silver → gold (fatti × prezzi, riepilogo per società)
├── ml/                   # Machine Learning (cascata XGBoost + etichettatura IA)
│   ├── engine.py         # event study / rendimento anomalo (CAPM; ARIMA opzionale)
│   ├── pipeline.py       # orchestra dataset → addestramento → metriche
│   ├── rotular.py        # etichettatura dei PDF tramite OpenAI gpt-4o-mini
│   ├── scoring.py        # punteggio fondamentale
│   ├── artefatos.py      # generazione degli artefatti onesti della demo
│   ├── loader.py         # lettura dei dati (gold / S3)
│   ├── config.py         # iperparametri della cascata
│   └── artefatos_seed/   # modello-seme incorporato in Docker (versionato)
├── dashboard/            # Applicazione web
│   ├── server.py         # Flask, porta 5050 (rotte + API JSON)
│   └── static/           # front-end
├── storage/
│   └── s3_manager/       # unico livello che parla con S3 (boto3)
├── scripts/              # Operazione e ingestione
│   ├── worker.py         # scheduler (systemd) — raccolta + riaddestramento
│   ├── market_data.py    # quotazioni (Yahoo Finance)
│   ├── fundamentalista.py# fondamentali (Fundamentus)
│   ├── download_pdfs_faltantes.py
│   ├── backfill_historico.py
│   ├── upload_s3.py
│   └── validar_pipeline.py
├── infra/                # Infrastruttura (EC2)
│   ├── setup_ec2.sh      # provisioning dell'ambiente
│   ├── radar-b3.service  # unit di systemd
│   └── s3_init.py        # crea il bucket / la struttura dei prefissi
├── tests/                # Test automatizzati
├── docs/                 # Documentazione
├── Dockerfile            # immagine Python 3.13-slim (uv sync)
├── buildspec.yml         # build su AWS CodeBuild → push su ECR
├── DEPLOY.md             # runbook di deploy AWS (risorse, comandi, teardown)
├── PLANO-INFRA.md        # passo passo ALB + ACM + CloudFront
├── pyproject.toml        # dipendenze (uv)
└── uv.lock               # lockfile (committato)
```

## 🛠 Stack

| Livello | Tecnologie |
|---|---|
| **Linguaggio** | Python 3.13, gestito con [`uv`](https://github.com/astral-sh/uv) |
| **Raccolta** | `requests` (API REST della B3), `yfinance`, Fundamentus |
| **Elaborazione** | `pandas`, `numpy`, `pyarrow` (Parquet), `pymupdf` (testo dei PDF) |
| **Machine Learning** | `scikit-learn`, `xgboost`, `joblib`; OpenAI `gpt-4o-mini` (etichettatura) |
| **Web** | `flask`, `flask-cors`, `plotly` |
| **Cloud** | AWS S3, EC2, ECS Fargate, ECR, CloudFront, ALB, IAM, CloudWatch |
| **Container / CI** | Docker, AWS CodeBuild (`buildspec.yml`) |
| **Pianificazione** | `schedule` + `systemd` |

## 💻 Esecuzione in locale

> Prerequisiti: **Python 3.13** e [**uv**](https://github.com/astral-sh/uv)
> (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
# 1. Installare le dipendenze (crea il .venv e risolve il lock)
uv sync

# 2. Configurare l'ambiente
cp .env.example .env        # regola il bucket / la regione se necessario
# per usare l'etichettatura IA, esporta la tua chiave OpenAI:
export OPENAI_API_KEY="sk-..."

# 3. (Facoltativo) Raccogliere dati per la macchina locale
uv run python scripts/market_data.py      # quotazioni
uv run python scraper/scraper.py          # fatti rilevanti della B3

# 4. Elaborare il data lake (bronze → silver → gold)
uv run python pipeline/silver_transform.py
uv run python pipeline/gold_transform.py

# 5. Avviare la dashboard
uv run python dashboard/server.py
# → http://localhost:5050
```

Per avviare l'ingestione continua (worker) in un secondo terminale:

```bash
uv run python scripts/worker.py
```

### Con Docker

```bash
docker build -t b3insight .
docker run -p 5050:5050 --env-file .env b3insight
# → http://localhost:5050
```

## 🔑 Variabili d'ambiente

Copia `.env.example` in `.env`. **Non** inserire mai chiavi nel repository.

| Variabile | Descrizione | Predefinito |
|---|---|---|
| `S3_BUCKET_NAME` | Bucket del data lake | `b3insight-data` |
| `AWS_DEFAULT_REGION` | Regione AWS | `us-east-1` |
| `FLASK_PORT` | Porta della dashboard | `5050` |
| `OPENAI_API_KEY` | Chiave OpenAI (etichettatura dei PDF) | — *(obbligatoria solo per l'IA)* |

> Su AWS, le credenziali S3 provengono dall'**IAM Role** di EC2/ECS — non ci sono
> chiavi AWS nel codice né nel `.env`. La chiave OpenAI è l'unica credenziale
> applicativa.

## ☁️ Deploy su AWS

Il flusso completo (risorse fornite, comandi operativi e **teardown**) è in
**[`DEPLOY.md`](./DEPLOY.md)**. Il passo passo per ALB + ACM + CloudFront è in
**[`PLANO-INFRA.md`](./PLANO-INFRA.md)**.

Riepilogo:

```
git push ──▶ CodeBuild (buildspec.yml) ──▶ ECR ──▶ ECS Fargate
                                                       │
                                              ALB :80/:443
                                                       │
                                           CloudFront (HTTPS pubblico)
```

- **Storage**: bucket S3 a livelli `bronze/ silver/ gold/`.
- **Ingestione**: `worker.py` come servizio `systemd` su EC2.
- **Presentazione**: la dashboard Flask gira su EC2 **e** su ECS/Fargate, entrambe
  leggendo da S3 tramite IAM Role.

> 💸 Ricordati di eseguire il **teardown** in `DEPLOY.md` dopo la consegna per
> evitare costi (EC2, Elastic IP, ECS, ALB, CloudFront).

## 🌐 Rotte della dashboard

| Rotta | Tipo | Descrizione |
|---|---|---|
| `/` | pagina | Ricerca società + panoramica |
| `/modelo` | pagina | Risultati e metriche del modello ML |
| `/treino` | pagina | Visualizzazione dell'addestramento |
| `/prever` | pagina | Previsione per un fatto rilevante |
| `/status` | health | Health check (HTTP 200) — usato dall'ALB |
| `/api/empresas` | JSON | Elenco delle società |
| `/api/precos/<ticker>` | JSON | Serie dei prezzi |
| `/api/fatos/<codigo_cvm>` | JSON | Fatti rilevanti della società |
| `/api/modelo`, `/api/modelo/<cvm>` | JSON | Output del modello |
| `/api/treino`, `/api/prever`, `/api/prever/sortear` | JSON | Schermate ML |

## 🔒 Sicurezza

- I segreti (`.env*`, `*.pem`, chiavi) sono coperti dal `.gitignore` — controlla
  prima di ogni `git push`.
- Le credenziali AWS sono fornite dall'**IAM Role**, non da chiavi nel codice.
- Il Security Group della demo apre le porte `22` e `5050` a `0.0.0.0/0`; in
  produzione, limita per IP di origine.
- Prima di rendere pubblico il repository, **ruota** qualsiasi chiave che sia mai
  esistita in locale (OpenAI, key pair EC2).

## 📜 Licenza

**Tutti i diritti riservati** — © 2026 Pedro Miranda e Luigi Ajello.

Questo è un lavoro accademico con codice *visibile ma non riutilizzabile*: puoi
**leggere e valutare** il codice, ma **non** puoi usarlo, copiarlo, modificarlo o
ridistribuirlo senza autorizzazione scritta degli autori. Vedi i termini completi
in [`LICENSE`](./LICENSE).

I dati a cui si accede appartengono alle rispettive fonti (B3, CVM, Yahoo Finance,
Fundamentus) e seguono i termini d'uso di ciascun fornitore. Progetto senza scopo
di lucro.

## 👥 Autori

- **Pedro Miranda**
- **Luigi Ajello**

Corso: *Cloud Computing & Big Data*.
