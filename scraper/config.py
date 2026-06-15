# config.py
# Configurações do scraper de Fatos Relevantes da B3

# 20 empresas do protótipo — 9 setores diferentes
# codigo_cvm: identificador único da empresa no sistema CVM/B3 (usado para filtrar a API)
EMPRESAS = [
    {"ticker": "PETR4", "nome": "Petrobras",        "setor": "Energia",        "codigo_cvm": "009512"},
    {"ticker": "VALE3", "nome": "Vale",              "setor": "Mineração",      "codigo_cvm": "004170"},
    {"ticker": "ITUB4", "nome": "Itaú Unibanco",    "setor": "Finanças",       "codigo_cvm": "019348"},
    {"ticker": "BBDC4", "nome": "Bradesco",          "setor": "Finanças",       "codigo_cvm": "000906"},
    {"ticker": "BBAS3", "nome": "Banco do Brasil",   "setor": "Finanças",       "codigo_cvm": "001023"},
    {"ticker": "ABEV3", "nome": "Ambev",             "setor": "Consumo",        "codigo_cvm": "023264"},
    {"ticker": "MGLU3", "nome": "Magazine Luiza",    "setor": "Varejo",         "codigo_cvm": "022470"},
    {"ticker": "WEGE3", "nome": "WEG",               "setor": "Indústria",      "codigo_cvm": "005410"},
    {"ticker": "EMBR3", "nome": "Embraer",           "setor": "Aeroespacial",   "codigo_cvm": "020087"},
    {"ticker": "JBSS3", "nome": "JBS",               "setor": "Alimentos",      "codigo_cvm": "080233"},
    {"ticker": "SUZB3", "nome": "Suzano",            "setor": "Papel/Celulose", "codigo_cvm": "013986"},
    {"ticker": "RENT3", "nome": "Localiza",          "setor": "Serviços",       "codigo_cvm": "019739"},
    {"ticker": "TOTS3", "nome": "TOTVS",             "setor": "Tecnologia",     "codigo_cvm": "019992"},
    {"ticker": "LREN3", "nome": "Lojas Renner",      "setor": "Varejo",         "codigo_cvm": "008133"},
    {"ticker": "ELET3", "nome": "Eletrobras",        "setor": "Energia",        "codigo_cvm": "002437"},
    {"ticker": "CSAN3", "nome": "Cosan",             "setor": "Energia",        "codigo_cvm": "019836"},
    {"ticker": "RAIL3", "nome": "Rumo Logística",    "setor": "Logística",      "codigo_cvm": "017450"},
    {"ticker": "RDOR3", "nome": "Rede D'Or",         "setor": "Saúde",          "codigo_cvm": "024821"},
    {"ticker": "HAPV3", "nome": "Hapvida",           "setor": "Saúde",          "codigo_cvm": "024392"},
    {"ticker": "BRFS3", "nome": "BRF",               "setor": "Alimentos",      "codigo_cvm": "016292"},
]

# Set de CVM codes para filtro rápido
CVM_CODES_ALVO = {e["codigo_cvm"].strip().lstrip("0") for e in EMPRESAS}

# Nome do bucket S3 (pode ser sobrescrito pela variável de ambiente S3_BUCKET_NAME)
import os as _os
S3_BUCKET = _os.environ.get("S3_BUCKET_NAME", "dados-b3-projeto")

# Período de coleta — 10 anos de histórico
DATA_INICIO = "01/01/2015"
DATA_FIM    = "31/12/2025"

# URL da interface Angular (mantida para referência)
B3_URL = "https://sistemaswebb3-listados.b3.com.br/reportsPeriodPage/material-facts?language=pt-BR"

# Endpoint REST real da B3 (descoberto via interceptação de rede)
# Recebe um JSON base64 com: language, dateInitial, dateFinal, pageNumber, pageSize
B3_API_URL = "https://sistemaswebb3-listados.b3.com.br/reportsPeriodProxy/ReportsPeriodCall/GetMaterialFacts"

# Pastas locais (espelham a estrutura do S3)
PASTA_FATOS = "dados/raw/fatos"
PASTA_PDFS  = "dados/pdfs"

# Prefixos Medallion
S3_PREFIX_BRONZE_FATOS      = "bronze/fatos"
S3_PREFIX_BRONZE_PDFS       = "bronze/pdfs"
S3_PREFIX_BRONZE_PRECOS     = "bronze/precos"
S3_PREFIX_SILVER_FATOS      = "silver/fatos"
S3_PREFIX_SILVER_PRECOS     = "silver/precos"
S3_PREFIX_GOLD_FATOS_PRECOS = "gold/fatos_precos"
S3_PREFIX_GOLD_RESUMO       = "gold/resumo_empresa"

# Comportamento do scraper
SLEEP_ENTRE_PAGINAS = 0.5  # segundos — pausa entre páginas para não sobrecarregar a B3
MAX_TENTATIVAS      = 3    # tentativas antes de desistir de uma página