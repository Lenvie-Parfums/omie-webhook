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
    sku_str = str(sku).strip()
    resp = chamar_omie(
        OMIE_PRODUTO_URL, "ConsultarProduto",
        APP_KEY_DESTINO, APP_SECRET_DESTINO,
        {"codigo": sku_str}
    )
    cod_id = resp.get("codigo_produto")
    if not cod_id:
        print(f"SKU {sku_str} nao encontrado diretamente na ATIVA.")
    return cod_id


# ==========================================================
# 3. IDEMPOTENCIA DO PEDIDO
# ==========================================================
def pedido_ja_existe_na_ativa(codigo_pedido_integracao):
    resp = chamar_omie(
        OMIE_PEDIDO_URL, "ConsultarPedido",
        APP_KEY_DESTINO, APP_SECRET_DESTINO,
        {"codigo_pedido_integracao": codigo_pedido_integracao}
    )
    if "pedido_venda_produto" in resp:
        print(f"Pedido {codigo_pedido_integracao} JA existe na ATIVA. Ignorando.")
        return True
    return False


# ==========================================================
# 4. TRANSFERENCIA DO PEDIDO (FRI -> ATIVA)
# ==========================================================
def transferir_pedido_omie(codigo_pedido_origem):
    bruto = chamar_omie(
        OMIE_PEDIDO_URL, "ConsultarPedido",
        APP_KEY_ORIGEM, APP_SECRET_ORIGEM,
        {"codigo_pedido": codigo_pedido_origem}
    )
    print(f"ConsultarPedido ORIGEM: {bruto}")

    if "faultstring" in bruto:
        print(f"Erro na origem: {bruto['faultstring']}")
        return False

    pedido = bruto.get("pedido_venda_produto", bruto)
    if "cabecalho" not in pedido:
        print("Pedido sem [cabecalho].")
        return False

    cod_int = pedido["cabecalho"].get("codigo_pedido_integracao") or str(codigo_pedido_origem)
    # Limita o tamanho do codigo de integracao do pedido para evitar extrapolamento
    codigo_integracao_destino = f"{cod_int}-ATV"[:30]
    
    if pedido_ja_existe_na_ativa(codigo_integracao_destino):
        return True

    id_origem_cliente = pedido["cabecalho"].get("codigo_cliente")
    print(f"Espelhando cliente origem {id_origem_cliente}...")
    id_destino_cliente = espelhar_cliente_destino(id_origem_cliente)
    if not id_destino_cliente:
        print("Nao foi possivel espelhar o cliente na ATIVA.")
        return False
    pedido["cabecalho"]["codigo_cliente"] = id_destino_cliente

    cab = pedido["cabecalho"]
    cab.pop("codigo_pedido", None)
    cab.pop("numero_pedido", None)
    cab.pop("codigo_cenario_impostos", None)
    cab["codigo_pedido_integracao"] = codigo_integracao_destino
    cab["etapa"] = ETAPA_ENTRADA_DESTINO
    cab["origem_pedido"] = "API"
    cab["codigo_parcela"] = CODIGO_PARCELA_PADRAO
    cab["qtde_parcelas"] = 1
    pedido.pop("lista_parcelas", None)
    cab.pop("codigo_transportadora", None)

    if "informacoes_adicionais" in pedido and isinstance(pedido["informacoes_adicionais"], dict):
        pedido["informacoes_adicionais"]["codigo_categoria"] = CATEGORIA_PADRAO
        pedido["informacoes_adicionais"]["codigo_conta_corrente"] = CONTA_CORRENTE_PADRAO
        pedido["informacoes_adicionais"].pop("codVend", None)
    else:
        pedido["informacoes_adicionais"] = {
            "codigo_categoria": CATEGORIA_PADRAO,
            "codigo_conta_corrente": CONTA_CORRENTE_PADRAO,
        }

    if "frete" in pedido and isinstance(pedido["frete"], dict):
        pedido["frete"].pop("codigo_transportadora", None)

    # LOOP DOS ITENS (Com geração de RG único e remoção do ID antigo)
    if "det" in pedido and isinstance(pedido["det"], list):
        for index, item in enumerate(pedido["det"], start=1):
            ide = item.get("ide", {})
            # Remove IDs de controle interno da base de origem
            ide.pop("codigo_item", None)
            ide.pop("codigo_item_pedido", None)
            
            # GERA O "RG" OBRIGATÓRIO PARA A API DO OMIE
            ide["codigo_item_integracao"] = f"{codigo_integracao_destino}-{index}"[:30]

            prod = item.get("produto", {})
            sku = prod.get("codigo")
            if sku:
                id_ativa = obter_id_produto_ativa(sku)
                if id_ativa:
                    prod["codigo_produto"] = id_ativa
                else:
                    print(f"❌ ERRO: SKU {sku} ({prod.get('descricao')}) nao cadastrado na ATIVA. Abortando pedido.")
                    return False
            prod.pop("valor_total", None)

            inf = item.get("inf_adic", {})
            inf.pop("codigo_local_estoque", None)
            inf.pop("codigo_cenario_impostos_item", None)
            if inf.get("codigo_categoria_item"):
                inf["codigo_categoria_item"] = CATEGORIA_PADRAO

    for chave in ["infoCadastro", "total_pedido", "departamentos"]:
        pedido.pop(chave, None)

    res = chamar_omie(
        OMIE_PEDIDO_URL, "IncluirPedido",
        APP_KEY_DESTINO, APP_SECRET_DESTINO, pedido
    )

    if "codigo_pedido" in res:
        print(f"✅ SUCESSO! Pedido transferido para a FILIADO ATACADO ES. Novo ID: {res['codigo_pedido']}")
        return True

    print(f"ERRO DO OMIE (ATIVA): {res}")
    return False


# ==========================================================
# 5. WEBHOOK
# ==========================================================
@app.route('/webhook/omie', methods=['POST'])
def receber_webhook():
    payload = request.json

    if payload and payload.get('ping'):
        return jsonify({"status": "ok"}), 200

    mensagem = payload.get('event', {}) if payload else {}
    codigo_pedido = mensagem.get('idPedido')
    etapa_atual = str(mensagem.get('etapa', ''))

    if etapa_atual == ETAPA_GATILHO:
        agora = time.time()
        with _lock:
            if codigo_pedido in _pedidos_em_processamento:
                print(f"Pedido {codigo_pedido} ja em processamento. Ignorando reenvio.")
                return jsonify({"status": "em_processamento"}), 200

            ultimo = _pedidos_ultimo_processo.get(codigo_pedido, 0)
            if agora - ultimo < COOLDOWN_PEDIDO:
                restante = int(COOLDOWN_PEDIDO - (agora - ultimo))
                print(f"Pedido {codigo_pedido} processado ha pouco. Cooldown {restante}s. Ignorando.")
                return jsonify({"status": "cooldown"}), 200

            _pedidos_em_processamento.add(codigo_pedido)
            _pedidos_ultimo_processo[codigo_pedido] = agora

        try:
            print(f"Transferindo pedido {codigo_pedido} (etapa {etapa_atual})...")
            sucesso = transferir_pedido_omie(codigo_pedido)
        finally:
            with _lock:
                _pedidos_em_processamento.discard(codigo_pedido)

        return jsonify({"status": "transferido" if sucesso else "erro"}), 200

    return jsonify({"status": "ignorado"}), 200


@app.route('/contas', methods=['GET'])
def listar_contas():
    url = "https://app.omie.com.br/api/v1/geral/contacorrente/"
    resp = chamar_omie(
        url, "ListarContasCorrentes",
        APP_KEY_DESTINO, APP_SECRET_DESTINO,
        {"pagina": 1, "registros_por_pagina": 100}
    )
    contas = resp.get("ListarContasCorrentes", resp.get("conta_corrente_lista", []))
    enxuto = [
        {
            "nCodCC": c.get("nCodCC"),
            "descricao": c.get("descricao"),
            "tipo": c.get("tipo_conta_corrente"),
        }
        for c in contas
    ]
    return jsonify({"raw": resp if not enxuto else None,
                    "total": len(enxuto), "contas": enxuto}), 200


@app.route('/', methods=['GET', 'HEAD'])
def home():
    return jsonify({
        "status": "online",
        "modo_leitura": "sob_demanda"
    }), 200


@app.route('/categorias', methods=['GET'])
def listar_categorias():
    url = "https://app.omie.com.br/api/v1/geral/categorias/"
    resp = chamar_omie(
        url, "ListarCategorias",
        APP_KEY_DESTINO, APP_SECRET_DESTINO,
        {"pagina": 1, "registros_por_pagina": 500}
    )
    cats = resp.get("categoria_cadastro", [])
    enxuto = [
        {"codigo": c.get("codigo"), "descricao": c.get("descricao")}
        for c in cats
    ]
    return jsonify({"total": len(enxuto), "categorias": enxuto}), 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
