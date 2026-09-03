import os
import time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================================
# 1. CREDENCIAIS OMIE (use variaveis de ambiente no Render)
# ==========================================================
APP_KEY_ORIGEM    = os.environ.get("APP_KEY_ORIGEM",    "1724630275368")
APP_SECRET_ORIGEM = os.environ.get("APP_SECRET_ORIGEM", "549a26b527f429912abf81f18570030e")
APP_KEY_DESTINO    = os.environ.get("APP_KEY_DESTINO",    "5102721230607")
APP_SECRET_DESTINO = os.environ.get("APP_SECRET_DESTINO", "e3e98a53e601102596075966c6c5f5a1")

OMIE_PEDIDO_URL  = "https://app.omie.com.br/api/v1/produtos/pedido/"
OMIE_CLIENTE_URL = "https://app.omie.com.br/api/v1/geral/clientes/"
OMIE_PRODUTO_URL = "https://app.omie.com.br/api/v1/geral/produtos/"

# ==========================================================
# 2. CONFIGURACOES
# ==========================================================
ETAPA_GATILHO        = "10"
ETAPAS_GATILHO       = {"10", "00"}  # 00 = codigo interno da Omie para Pedido de Venda (etapa 10)
ETAPA_ENTRADA_DESTINO = "10"
CATEGORIA_PADRAO      = os.environ.get("CATEGORIA_PADRAO", "1.01.02")
CONTA_CORRENTE_PADRAO = int(os.environ.get("CONTA_CORRENTE_PADRAO", "6760726795"))
CODIGO_PARCELA_PADRAO = os.environ.get("CODIGO_PARCELA_PADRAO", "000")
COOLDOWN_PEDIDO       = int(os.environ.get("COOLDOWN_PEDIDO", "300"))
ESTADOS_PERMITIDOS    = set(os.environ.get("ESTADOS_PERMITIDOS", "ES,MG").upper().split(","))

# ==========================================================
# 3. CONTROLE ANTI-LOOP
# ==========================================================
_pedidos_em_processamento = set()
_pedidos_ultimo_processo  = {}
_lock = threading.Lock()

# Cache de produtos em memoria: SKU -> codigo_produto da ATIVA
# Preenchido na primeira vez que cada SKU e consultado.
_cache_produtos = {}
_cache_clientes = {}  # codigo_cliente_omie -> dict completo do cliente


# ==========================================================
# 4. HELPER OMIE
# ==========================================================
def chamar_omie(url, call, app_key, app_secret, param):
    payload = {"call": call, "app_key": app_key, "app_secret": app_secret, "param": [param]}
    try:
        resp = requests.post(url, json=payload, timeout=60).json()
    except Exception as e:
        print(f"Falha de rede em {call}: {e}")
        return {"faultstring": str(e)}
    fault = str(resp.get("faultstring", "")) if isinstance(resp, dict) else ""
    if "MISUSE_API_PROCESS" in str(resp) or "API bloqueada" in fault:
        print(f"!!! API OMIE BLOQUEADA em {call}: {fault}")
    elif "REDUNDANT" in str(resp):
        print(f"Consumo redundante em {call}. Aguarde antes de repetir.")
    return resp


# ==========================================================
# 5. CLIENTE (FRI -> ATIVA)
# ==========================================================
def consultar_cliente_origem(codigo_cliente_origem):
    """Le o cadastro do cliente na FRI. Usa cache para evitar chamadas repetidas."""
    codigo_cliente_origem = int(codigo_cliente_origem)
    if codigo_cliente_origem in _cache_clientes:
        return _cache_clientes[codigo_cliente_origem]
    cli = chamar_omie(OMIE_CLIENTE_URL, "ConsultarCliente",
                      APP_KEY_ORIGEM, APP_SECRET_ORIGEM,
                      {"codigo_cliente_omie": codigo_cliente_origem}) or {}
    if cli and not cli.get("faultstring"):
        _cache_clientes[codigo_cliente_origem] = cli
    return cli


def espelhar_cliente_destino(cli):
    """
    Recebe o cadastro ja lido da FRI e cria/atualiza no CNPJ 004.
    Se o CNPJ ja existir com outro codigo de integracao, busca o ID existente.
    Retorna o codigo_cliente_omie do destino.
    """
    cnpj_cpf = cli.get("cnpj_cpf")
    if not cnpj_cpf:
        print("Cliente sem CNPJ/CPF. Abortando.")
        return None

    cnpj_limpo = "".join(filter(str.isalnum, cnpj_cpf))
    cod_int    = f"FRI-{cnpj_limpo}"

    campos = [
        "razao_social", "nome_fantasia", "cnpj_cpf", "email",
        "telefone1_ddd", "telefone1_numero",
        "endereco", "endereco_numero", "complemento", "bairro",
        "cidade", "estado", "cep", "cidade_ibge", "codigo_pais",
        "inscricao_estadual", "inscricao_municipal",
        "pessoa_fisica", "optante_simples_nacional",
        "contribuinte", "produtor_rural",
    ]
    upsert = {"codigo_cliente_integracao": cod_int}
    for c in campos:
        if cli.get(c) not in (None, ""):
            upsert[c] = cli[c]

    res = chamar_omie(OMIE_CLIENTE_URL, "UpsertCliente",
                      APP_KEY_DESTINO, APP_SECRET_DESTINO, upsert)

    id_destino = res.get("codigo_cliente_omie")
    if id_destino:
        print(f"Cliente espelhado na ATIVA. ID: {id_destino}")
        return id_destino

    # UpsertCliente falhou — pode ser CNPJ ja cadastrado com outro cod. integracao.
    # Tenta buscar o cliente pelo CNPJ diretamente.
    fault = res.get("faultstring", "")
    if "já cadastrado" in fault or "ja cadastrado" in fault.lower():
        print(f"Cliente ja existe na ATIVA com outro codigo de integracao. Buscando por CNPJ...")
        busca = chamar_omie(
            OMIE_CLIENTE_URL, "ListarClientes",
            APP_KEY_DESTINO, APP_SECRET_DESTINO,
            {"pagina": 1, "registros_por_pagina": 5,
             "clientesFiltro": {"cnpj_cpf": cnpj_limpo}}
        )
        clientes = busca.get("clientes_cadastro", [])
        if clientes:
            id_destino = clientes[0].get("codigo_cliente_omie")
            print(f"Cliente encontrado na ATIVA por CNPJ. ID: {id_destino}")
            return id_destino

    print(f"UpsertCliente falhou: {res}")
    return None


# ==========================================================
# 6. RESOLVER PRODUTO NA ATIVA (cache por SKU)
# ==========================================================
def resolver_produto_ativa(sku):
    sku = str(sku).strip()
    if sku in _cache_produtos:
        return _cache_produtos[sku]

    # Busca produto pelo codigo (SKU) via ConsultarProduto na ATIVA
    resp = chamar_omie(OMIE_PRODUTO_URL, "ConsultarProduto",
                       APP_KEY_DESTINO, APP_SECRET_DESTINO,
                       {"codigo": sku})
    cod = resp.get("codigo_produto")
    if cod:
        _cache_produtos[sku] = cod
        print(f"Produto SKU {sku} -> ID ATIVA {cod}")
        return cod

    # Nao encontrado na ATIVA — tenta buscar na FRI e replicar
    print(f"SKU {sku} nao encontrado na ATIVA. Buscando na FRI para replicar...")
    prod_fri = chamar_omie(OMIE_PRODUTO_URL, "ConsultarProduto",
                           APP_KEY_ORIGEM, APP_SECRET_ORIGEM,
                           {"codigo": sku})
    if "faultstring" in prod_fri or not prod_fri.get("codigo_produto"):
        print(f"SKU {sku} tambem nao encontrado na FRI. Nao e possivel replicar.")
        return None

    # Monta payload para replicar o produto na ATIVA
    campos = [
        "codigo", "descricao", "tipoItem", "ncm", "cest", "ean",
        "unidade", "valor_unitario", "peso_liq", "peso_bruto",
        "altura", "largura", "profundidade", "marca", "descr_detalhada",
        "unidade_tributavel", "quantidade_tributavel",
    ]
    payload_prod = {}
    for c in campos:
        if prod_fri.get(c) not in (None, "", 0):
            payload_prod[c] = prod_fri[c]

    # Familia LENVIE no CNPJ 004
    if prod_fri.get("codigo_familia"):
        payload_prod["codigo_familia"] = 7243824999

    payload_prod["codigo_produto_integracao"] = sku
    payload_prod["origem_mercadoria"] = (
        prod_fri.get("recomendacoes_fiscais", {}).get("origem_mercadoria", "0")
    )

    res_upsert = chamar_omie(OMIE_PRODUTO_URL, "UpsertProduto",
                             APP_KEY_DESTINO, APP_SECRET_DESTINO, payload_prod)
    cod_novo = res_upsert.get("codigo_produto")
    if cod_novo:
        _cache_produtos[sku] = cod_novo
        print(f"SKU {sku} replicado automaticamente na ATIVA. ID: {cod_novo}")
        return cod_novo

    print(f"Falha ao replicar SKU {sku} na ATIVA: {res_upsert.get('faultstring', res_upsert)}")
    return None


# ==========================================================
# 7. IDEMPOTENCIA
# ==========================================================
def pedido_ja_existe_na_ativa(codigo_integracao):
    resp = chamar_omie(OMIE_PEDIDO_URL, "ConsultarPedido",
                       APP_KEY_DESTINO, APP_SECRET_DESTINO,
                       {"codigo_pedido_integracao": codigo_integracao})
    if "pedido_venda_produto" in resp:
        print(f"Pedido {codigo_integracao} ja existe na ATIVA. Ignorando.")
        return True
    return False


# ==========================================================
# 8. TRANSFERENCIA DO PEDIDO
# ==========================================================
def transferir_pedido_omie(codigo_pedido_origem):
    # 8.1 Consulta na FRI
    bruto = chamar_omie(OMIE_PEDIDO_URL, "ConsultarPedido",
                        APP_KEY_ORIGEM, APP_SECRET_ORIGEM,
                        {"codigo_pedido": codigo_pedido_origem})
    if "faultstring" in bruto:
        print(f"Erro ao consultar pedido: {bruto['faultstring']}")
        return False

    pedido = bruto.get("pedido_venda_produto", bruto)
    if "cabecalho" not in pedido:
        print("Pedido sem [cabecalho].")
        return False

    if not pedido.get("det"):
        print(f"Pedido {codigo_pedido_origem} sem itens. Ignorando.")
        return True

    # 8.2 Idempotencia
    cod_int = pedido["cabecalho"].get("codigo_pedido_integracao") or str(codigo_pedido_origem)
    cod_int_destino = f"{cod_int}-ATIVA"
    if pedido_ja_existe_na_ativa(cod_int_destino):
        return True

    # 8.3 Le e espelha o cliente (estado ja foi verificado no webhook)
    id_cliente_origem = pedido["cabecalho"].get("codigo_cliente")
    cli = consultar_cliente_origem(id_cliente_origem)
    estado = str(cli.get("estado", "")).upper().strip()
    print(f"Processando pedido de cliente no estado [{estado}]...")

    id_cliente_destino = espelhar_cliente_destino(cli)
    if not id_cliente_destino:
        print("Nao foi possivel espelhar o cliente.")
        return False

    # 8.4 Ajusta cabecalho
    cab = pedido["cabecalho"]
    cab["codigo_cliente"]            = id_cliente_destino
    cab["codigo_pedido_integracao"]  = cod_int_destino
    cab["etapa"]                     = ETAPA_ENTRADA_DESTINO
    cab["origem_pedido"]             = "API"
    cab["codigo_parcela"]            = CODIGO_PARCELA_PADRAO
    cab["qtde_parcelas"]             = 1
    cab.pop("codigo_pedido",          None)
    cab.pop("numero_pedido",          None)
    cab.pop("codigo_cenario_impostos",None)
    cab.pop("codigo_transportadora",  None)
    pedido.pop("lista_parcelas",      None)

    # 8.5 Informacoes adicionais
    inf_ad = pedido.get("informacoes_adicionais")
    if isinstance(inf_ad, dict):
        inf_ad["codigo_categoria"]     = CATEGORIA_PADRAO
        inf_ad["codigo_conta_corrente"]= CONTA_CORRENTE_PADRAO
        inf_ad.pop("codVend",            None)
    else:
        pedido["informacoes_adicionais"] = {
            "codigo_categoria":      CATEGORIA_PADRAO,
            "codigo_conta_corrente": CONTA_CORRENTE_PADRAO,
        }

    # 8.6 Frete
    if isinstance(pedido.get("frete"), dict):
        pedido["frete"].pop("codigo_transportadora", None)

    # 8.7 Itens
    for item in pedido.get("det", []):
        ide = item.get("ide", {})
        ide.pop("codigo_item_pedido", None)
        if not ide.get("codigo_item_integracao"):
            ide["codigo_item_integracao"] = str(ide.get("codigo_item", ""))[:30]

        prod = item.get("produto", {})
        sku  = prod.get("codigo")
        if sku:
            id_prod = resolver_produto_ativa(sku)
            if id_prod:
                prod["codigo_produto"] = id_prod
            else:
                print(f"ABORTANDO: SKU {sku} nao resolvido nem na FRI nem na ATIVA.")
                return False
        prod.pop("valor_total", None)

        inf = item.get("inf_adic", {})
        inf.pop("codigo_local_estoque",         None)
        inf.pop("codigo_cenario_impostos_item",  None)
        if inf.get("codigo_categoria_item"):
            inf["codigo_categoria_item"] = CATEGORIA_PADRAO
        # Preserva obs_item (contem lote/fabricacao/validade da FRI)
        # para o webhook-rastreabilidade nao cancelar o pedido no destino.
        # O obs_item ja vem no item original e nao e removido.

    # 8.8 Remove blocos calculados
    for chave in ["infoCadastro", "total_pedido", "departamentos"]:
        pedido.pop(chave, None)

    # 8.9 Inclui na ATIVA
    res = chamar_omie(OMIE_PEDIDO_URL, "IncluirPedido",
                      APP_KEY_DESTINO, APP_SECRET_DESTINO, pedido)
    if "codigo_pedido" in res:
        print(f"SUCESSO! Pedido gravado na ATIVA. Novo ID: {res['codigo_pedido']}")
        return True

    print(f"ERRO DO OMIE (ATIVA): {res}")
    return False


# ==========================================================
# 9. WEBHOOK
# ==========================================================
@app.route('/webhook/omie', methods=['POST'])
def receber_webhook():
    payload = request.json

    if payload and payload.get('ping'):
        return jsonify({"status": "ok"}), 200

    mensagem    = payload.get('event', {}) if payload else {}
    print(f"PAYLOAD: topic={payload.get('topic')} etapa={mensagem.get('etapa')} idPedido={mensagem.get('idPedido')} idCliente={mensagem.get('idCliente')}")
    topic       = str(payload.get('topic', '')).lower() if payload else ''
    codigo_pedido = mensagem.get('idPedido') or mensagem.get('codigo_pedido')
    etapa_atual = str(mensagem.get('etapa', ''))
    id_cliente_webhook = mensagem.get('idCliente')

    eh_inclusao = 'incluido' in topic or 'incluida' in topic

    if etapa_atual in ETAPAS_GATILHO or eh_inclusao:
        # Filtra por estado ANTES de qualquer chamada pesada (ConsultarPedido).
        # O idCliente ja vem no payload do webhook — apenas ConsultarCliente.
        if ESTADOS_PERMITIDOS and id_cliente_webhook:
            cli_check = consultar_cliente_origem(id_cliente_webhook)
            estado_check = str(cli_check.get("estado", "")).upper().strip()
            if estado_check not in ESTADOS_PERMITIDOS:
                print(f"Pedido {codigo_pedido} ignorado: estado [{estado_check}] "
                      f"fora da lista {ESTADOS_PERMITIDOS}.")
                return jsonify({"status": "ignorado_estado"}), 200

        agora = time.time()
        with _lock:
            if codigo_pedido in _pedidos_em_processamento:
                return jsonify({"status": "em_processamento"}), 200
            ultimo = _pedidos_ultimo_processo.get(codigo_pedido, 0)
            if agora - ultimo < COOLDOWN_PEDIDO:
                restante = int(COOLDOWN_PEDIDO - (agora - ultimo))
                print(f"Pedido {codigo_pedido} em cooldown ({restante}s).")
                return jsonify({"status": "cooldown"}), 200
            _pedidos_em_processamento.add(codigo_pedido)
            _pedidos_ultimo_processo[codigo_pedido] = agora

        try:
            print(f"Transferindo pedido {codigo_pedido} (etapa {etapa_atual} | {topic})...")
            sucesso = transferir_pedido_omie(codigo_pedido)
        finally:
            with _lock:
                _pedidos_em_processamento.discard(codigo_pedido)

        return jsonify({"status": "transferido" if sucesso else "erro"}), 200

    return jsonify({"status": "ignorado"}), 200


# ==========================================================
# 10. ROTAS AUXILIARES
# ==========================================================
@app.route('/', methods=['GET', 'HEAD'])
def home():
    return jsonify({"status": "online", "cache_produtos": len(_cache_produtos)}), 200


@app.route('/categorias', methods=['GET'])
def listar_categorias():
    url  = "https://app.omie.com.br/api/v1/geral/categorias/"
    resp = chamar_omie(url, "ListarCategorias", APP_KEY_DESTINO, APP_SECRET_DESTINO,
                       {"pagina": 1, "registros_por_pagina": 500})
    cats = [{"codigo": c.get("codigo"), "descricao": c.get("descricao")}
            for c in resp.get("categoria_cadastro", [])]
    return jsonify({"total": len(cats), "categorias": cats}), 200


@app.route('/contas', methods=['GET'])
def listar_contas():
    url  = "https://app.omie.com.br/api/v1/geral/contacorrente/"
    resp = chamar_omie(url, "ListarContasCorrentes", APP_KEY_DESTINO, APP_SECRET_DESTINO,
                       {"pagina": 1, "registros_por_pagina": 100})
    contas = resp.get("ListarContasCorrentes", resp.get("conta_corrente_lista", []))
    enxuto = [{"nCodCC": c.get("nCodCC"), "descricao": c.get("descricao")} for c in contas]
    return jsonify({"raw": resp if not enxuto else None, "total": len(enxuto), "contas": enxuto}), 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
