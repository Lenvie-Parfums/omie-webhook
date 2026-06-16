import os
import time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Controle de deduplicacao em memoria: evita reprocessar o mesmo pedido
# em loop (o que dispara o bloqueio MISUSE_API_PROCESS da Omie).
_pedidos_em_processamento = set()
_pedidos_ultimo_processo = {}
_lock = threading.Lock()
# Tempo minimo (segundos) antes de aceitar reprocessar o mesmo pedido.
COOLDOWN_PEDIDO = int(os.environ.get("COOLDOWN_PEDIDO", "300"))

# ==========================================================
# 1. CREDENCIAIS OMIE  (use variaveis de ambiente no Render)
# ==========================================================
APP_KEY_ORIGEM = os.environ.get("APP_KEY_ORIGEM", "1724630275368")
APP_SECRET_ORIGEM = os.environ.get("APP_SECRET_ORIGEM", "549a26b527f429912abf81f18570030e")

APP_KEY_DESTINO = os.environ.get("APP_KEY_DESTINO", "5102721230607")
APP_SECRET_DESTINO = os.environ.get("APP_SECRET_DESTINO", "e3e98a53e601102596075966c6c5f5a1")

OMIE_PEDIDO_URL = "https://app.omie.com.br/api/v1/produtos/pedido/"
OMIE_CLIENTE_URL = "https://app.omie.com.br/api/v1/geral/clientes/"
OMIE_PRODUTO_URL = "https://app.omie.com.br/api/v1/geral/produtos/"

ETAPA_GATILHO = "80"
ETAPA_ENTRADA_DESTINO = "10"
CATEGORIA_PADRAO = os.environ.get("CATEGORIA_PADRAO", "1.01.02")
CONTA_CORRENTE_PADRAO = int(os.environ.get("CONTA_CORRENTE_PADRAO", "6760726795"))
CODIGO_PARCELA_PADRAO = os.environ.get("CODIGO_PARCELA_PADRAO", "000")


def chamar_omie(url, call, app_key, app_secret, param):
    payload = {
        "call": call,
        "app_key": app_key,
        "app_secret": app_secret,
        "param": [param]
    }
    try:
        resp = requests.post(url, json=payload, timeout=60).json()
    except Exception as e:
        print(f"Falha de rede em {call}: {e}")
        return {"faultstring": str(e)}

    fault = str(resp.get("faultstring", "")) if isinstance(resp, dict) else ""
    if "MISUSE_API_PROCESS" in str(resp) or "API bloqueada" in fault:
        print(f"!!! API OMIE BLOQUEADA em {call}. PARE os testes e aguarde. Resposta: {fault}")
    elif "REDUNDANT" in str(resp):
        print(f"Consumo redundante em {call}. Aguarde antes de repetir.")

    return resp


# ==========================================================
# 2. ESPELHAR O CLIENTE E PRODUTO (ORIGEM -> ATIVA)
# ==========================================================
def espelhar_cliente_destino(codigo_cliente_origem):
    cli = chamar_omie(
        OMIE_CLIENTE_URL, "ConsultarCliente",
        APP_KEY_ORIGEM, APP_SECRET_ORIGEM,
        {"codigo_cliente_omie": codigo_cliente_origem}
    )
    print(f"ConsultarCliente ORIGEM: {cli}")

    cnpj_cpf = cli.get("cnpj_cpf")
    if not cnpj_cpf:
        print("Cliente da origem sem CNPJ/CPF. Abortando.")
        return None

    cnpj_limpo = "".join(filter(str.isalnum, cnpj_cpf))
    cod_int_cliente = f"FRI-{cnpj_limpo}"

    campos = [
        "razao_social", "nome_fantasia", "cnpj_cpf", "email",
        "telefone1_ddd", "telefone1_numero",
        "endereco", "endereco_numero", "complemento", "bairro",
        "cidade", "estado", "cep", "cidade_ibge", "codigo_pais",
        "inscricao_estadual", "inscricao_municipal",
        "pessoa_fisica", "optante_simples_nacional",
        "contribuinte", "produtor_rural",
    ]
    upsert = {"codigo_cliente_integracao": cod_int_cliente}
    for c in campos:
        if cli.get(c) not in (None, ""):
            upsert[c] = cli[c]

    if str(upsert.get("razao_social", "")).strip().lower().startswith("cliente"):
        print("AVISO: razao social comeca com 'Cliente' - Omie bloqueia faturamento.")

    res = chamar_omie(
        OMIE_CLIENTE_URL, "UpsertCliente",
        APP_KEY_DESTINO, APP_SECRET_DESTINO, upsert
    )
    print(f"UpsertCliente ATIVA: {res}")

    id_destino = res.get("codigo_cliente_omie")
    if id_destino:
        print(f"Cliente espelhado na ATIVA. ID destino: {id_destino}")
        return id_destino

    print(f"UpsertCliente nao retornou ID. Resposta: {res}")
    return None

def obter_id_produto_ativa(sku):
    """Busca cirurgicamente um produto na ATIVA pelo seu SKU (codigo texto)."""
    sku_str = str(sku).
