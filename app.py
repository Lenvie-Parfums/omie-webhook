import hmac
import json
import re
import os
import time
import threading
import requests
from flask import Flask, request, jsonify
from portal_ativa import bridge

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
OMIE_VENDEDOR_URL = "https://app.omie.com.br/api/v1/geral/vendedores/"

# ==========================================================
# 2. CONFIGURACOES
# ==========================================================
ETAPAS_GATILHO       = {"10", "00"}  # 00 = codigo interno da Omie para Pedido de Venda (etapa 10)
ETAPA_ENTRADA_DESTINO = "10"
CATEGORIA_PADRAO      = os.environ.get("CATEGORIA_PADRAO", "1.01.02")
CONTA_CORRENTE_PADRAO = int(os.environ.get("CONTA_CORRENTE_PADRAO", "6760726795"))
CODIGO_PARCELA_PADRAO = os.environ.get("CODIGO_PARCELA_PADRAO", "000")
COOLDOWN_PEDIDO       = int(os.environ.get("COOLDOWN_PEDIDO", "300"))
ESTADOS_PERMITIDOS    = {"ES", "MG"} & {uf.strip().upper() for uf in os.environ.get("ESTADOS_PERMITIDOS", "ES,MG").split(",")}

# ==========================================================
# 3. CONTROLE ANTI-LOOP
# ==========================================================
_pedidos_em_processamento = set()
_pedidos_ultimo_processo  = {}
_lock = threading.Lock()

# Cache de produtos em memoria: SKU -> codigo_produto da ATIVA
# Preenchido na primeira vez que cada SKU e consultado.
_cache_produtos = {}
_cache_referencias = {}
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
def pedido_existente_na_ativa(codigo_integracao, exigir_resposta=False):
    resp = chamar_omie(OMIE_PEDIDO_URL, "ConsultarPedido",
                       APP_KEY_DESTINO, APP_SECRET_DESTINO,
                       {"codigo_pedido_integracao": codigo_integracao})
    pedido = resp.get("pedido_venda_produto", resp).get("cabecalho")
    if exigir_resposta and not pedido:
        falha = str(resp.get("faultstring") or "")
        ausencia = re.search(r"\bpedido\b.*\bn[ãa]o (?:encontrad[oa]|cadastrad[oa]|existe|localizad[oa])", falha, re.I)
        if not ausencia or re.search(r"REDUNDANT|MISUSE_API_PROCESS|bloquead[ao]|limite", falha, re.I):
            raise ValueError("Nao foi possivel confirmar a ausencia na 004. Nenhuma inclusao foi solicitada.")
    if exigir_resposta and pedido and (
        str(pedido.get("codigo_pedido_integracao")) != codigo_integracao or not pedido.get("codigo_pedido")
    ):
        raise ValueError("A Omie 004 retornou outro pedido. Nenhuma inclusao foi solicitada.")
    return pedido


# ==========================================================
# 8. TRANSFERENCIA DO PEDIDO
# ==========================================================
def consultar_referencia(url, chamada, key, secret, parametros, aceitar_vazio=False):
    time.sleep(1.1)
    resposta = chamar_omie(url, chamada, key, secret, parametros)
    falha = resposta.get("faultstring")
    if aceitar_vazio and falha and re.fullmatch(
        r"(?:ERROR:\s*)?N[aã]o existem registros para a p[aá]gina \[1\][!.]?", falha.strip(), re.I,
    ):
        return {}
    if falha or resposta.get("faultcode") or not resposta:
        raise ValueError(f"{chamada} recusada pela Omie: {falha or 'resposta invalida'}.")
    return resposta


def resolver_referencia(grupo, codigo):
    chave = (grupo, str(codigo))
    agora = time.monotonic()
    cache = _cache_referencias.get(chave)
    if cache and agora < cache[0]:
        return cache[1]

    if grupo == "vendedores":
        origem = consultar_referencia(
            OMIE_VENDEDOR_URL, "ConsultarVendedor", APP_KEY_ORIGEM, APP_SECRET_ORIGEM,
            {"codigo": int(codigo)},
        )
        nome = str(origem.get("nome") or "").strip()
        email = str(origem.get("email") or "").strip().casefold()
        if not nome:
            raise ValueError("Vendedor sem nome na 001.")
        resposta = consultar_referencia(
            OMIE_VENDEDOR_URL, "ListarVendedores", APP_KEY_DESTINO, APP_SECRET_DESTINO,
            {"pagina": 1, "registros_por_pagina": 100, "filtrar_por_nome": nome},
            aceitar_vazio=True,
        )
        candidatos = [
            v["codigo"] for v in resposta.get("cadastro", [])
            if v.get("codigo") and v.get("inativo") != "S"
            and str(v.get("nome") or "").strip().casefold() == nome.casefold()
            and (not email or str(v.get("email") or "").strip().casefold() == email)
        ]
    else:
        origem = consultar_referencia(
            OMIE_CLIENTE_URL, "ConsultarCliente", APP_KEY_ORIGEM, APP_SECRET_ORIGEM,
            {"codigo_cliente_omie": int(codigo)},
        )
        nome = str(origem.get("razao_social") or origem.get("nome_fantasia") or "").strip()
        documento = re.sub(r"[^A-Za-z0-9]", "", str(origem.get("cnpj_cpf") or ""))
        if not documento:
            raise ValueError("Transportadora sem CNPJ/CPF na 001.")
        resposta = consultar_referencia(
            OMIE_CLIENTE_URL, "ListarClientes", APP_KEY_DESTINO, APP_SECRET_DESTINO,
            {"pagina": 1, "registros_por_pagina": 100, "clientesFiltro": {"cnpj_cpf": documento}},
            aceitar_vazio=True,
        )
        candidatos = [
            c["codigo_cliente_omie"] for c in resposta.get("clientes_cadastro", [])
            if c.get("codigo_cliente_omie") and c.get("inativo") != "S"
            and re.sub(r"[^A-Za-z0-9]", "", str(c.get("cnpj_cpf") or "")).upper() == documento.upper()
        ]

    resultado = (candidatos[0] if len(candidatos) == 1 and int(resposta.get("total_de_paginas") or 1) == 1 else None, nome)
    _cache_referencias[chave] = (agora + (3600 if resultado[0] else 300), resultado)
    return resultado


def mapear_cadastro(pedido, bloco, campo, mapa, grupo, rotulo):
    origem = bloco.get(campo)
    mapear_campo(bloco, campo, mapa, grupo)
    if not origem or campo in bloco:
        return
    destino, nome = resolver_referencia(grupo, origem)
    if destino:
        bloco[campo] = destino
        return
    if nome:
        observacoes = pedido.setdefault("observacoes", {})
        nota = f"{rotulo} na 001: {nome}"
        existente = observacoes.get("obs_venda") or ""
        observacoes["obs_venda"] = f"{existente} | {nota}" if existente else nota
    print(f"{rotulo} da 001 sem correspondencia unica na 004.")


def mapear_campo(bloco, campo, mapa, grupo):
    codigo_origem = bloco.pop(campo, None)
    codigo_destino = mapa.get(grupo, {}).get(str(codigo_origem))
    if codigo_destino is not None:
        bloco[campo] = codigo_destino


def preparar_dados_destino(pedido, mapa):
    cab = pedido["cabecalho"]
    codigo_parcela = str(cab.get("codigo_parcela") or "").strip()
    parcelas = (pedido.pop("lista_parcelas", None) or {}).get("parcela") or []
    codigo_mapeado = mapa.get("parcelas", {}).get(codigo_parcela)

    if parcelas:
        campos = (
            "numero_parcela", "valor", "percentual", "data_vencimento", "quantidade_dias",
            "tipo_documento", "meio_pagamento", "descr_meio_pagamento", "nsu",
            "nao_gerar_boleto", "parcela_adiantamento", "bandeira", "cnpj_fin",
        )
        parcelas_destino = []
        for parcela in parcelas:
            destino = {campo: parcela[campo] for campo in campos if parcela.get(campo) not in (None, "")}
            if destino.get("parcela_adiantamento") == "S":
                for campo, grupo in (("categoria_adiantamento", "categorias"), ("conta_corrente_adiantamento", "contas_correntes")):
                    codigo = mapa.get(grupo, {}).get(str(parcela.get(campo)))
                    if codigo is None:
                        raise ValueError(f"Parcela de adiantamento sem de-para de {grupo}.")
                    destino[campo] = codigo
            if not destino.get("numero_parcela") or not (
                destino.get("data_vencimento") or "quantidade_dias" in destino
            ) or not ("valor" in destino or "percentual" in destino):
                raise ValueError("Parcela sem numero, vencimento ou valor/percentual na 001.")
            parcelas_destino.append(destino)
        if len({p["numero_parcela"] for p in parcelas_destino}) != len(parcelas_destino):
            raise ValueError("Numeros de parcela repetidos na 001.")
        if cab.get("qtde_parcelas") and int(cab["qtde_parcelas"]) != len(parcelas_destino):
            raise ValueError("Quantidade de parcelas difere da lista retornada pela 001.")
        cab["codigo_parcela"] = "999"
        cab["qtde_parcelas"] = len(parcelas_destino)
        pedido["lista_parcelas"] = {"parcela": parcelas_destino}
    elif codigo_mapeado is not None and str(codigo_mapeado) != "999":
        cab["codigo_parcela"] = str(codigo_mapeado)
    elif codigo_parcela == "000" and str(cab.get("qtde_parcelas") or "1") == "1" and CODIGO_PARCELA_PADRAO == "000":
        cab["codigo_parcela"] = "000"
        cab["qtde_parcelas"] = 1
    else:
        raise ValueError("Condicao de pagamento sem parcelas na 001 ou de-para para a 004.")

    mapear_campo(cab, "codigo_cenario_impostos", mapa, "cenarios_fiscais")
    frete = pedido.get("frete")
    if not isinstance(frete, dict):
        frete = {}
    codigo_transportadora = cab.pop("codigo_transportadora", None)
    if codigo_transportadora and not frete.get("codigo_transportadora"):
        frete["codigo_transportadora"] = codigo_transportadora
    mapear_cadastro(pedido, frete, "codigo_transportadora", mapa, "transportadoras", "Transportadora")
    frete.pop("codigo_transportadora_integracao", None)
    if frete:
        pedido["frete"] = frete

    adicionais = pedido.get("informacoes_adicionais")
    if not isinstance(adicionais, dict):
        adicionais = {}
    for campo, grupo in (
        ("codigo_categoria", "categorias"), ("codigo_conta_corrente", "contas_correntes"),
        ("codProj", "projetos"),
    ):
        mapear_campo(adicionais, campo, mapa, grupo)
    mapear_cadastro(pedido, adicionais, "codVend", mapa, "vendedores", "Vendedor")
    adicionais.setdefault("codigo_categoria", CATEGORIA_PADRAO)
    adicionais.setdefault("codigo_conta_corrente", CONTA_CORRENTE_PADRAO)
    pedido["informacoes_adicionais"] = adicionais

    departamentos = pedido.pop("departamentos", None) or []
    for departamento in departamentos:
        mapear_campo(departamento, "cCodDepto", mapa, "departamentos")
    # Mantem o rateio completo apenas quando todos os departamentos tem equivalente.
    if departamentos and all(departamento.get("cCodDepto") for departamento in departamentos):
        pedido["departamentos"] = departamentos

    for item in pedido.get("det", []):
        inf = item.get("inf_adic") or {}
        for campo, grupo in (
            ("codigo_local_estoque", "locais_estoque"),
            ("codigo_cenario_impostos_item", "cenarios_fiscais"),
            ("codigo_categoria_item", "categorias"),
        ):
            if campo in inf and inf[campo]:
                mapear_campo(inf, campo, mapa, grupo)
                if campo == "codigo_categoria_item":
                    inf.setdefault(campo, CATEGORIA_PADRAO)


def transferir_pedido_omie(codigo_pedido_origem, audit=None, reenvio=False):
    if audit is None:
        audit = {}
    audit["detail"] = "A Omie nao confirmou a integracao."
    # 8.1 Consulta na FRI
    bruto = chamar_omie(OMIE_PEDIDO_URL, "ConsultarPedido",
                        APP_KEY_ORIGEM, APP_SECRET_ORIGEM,
                        {"codigo_pedido": codigo_pedido_origem})
    if "faultstring" in bruto:
        audit["detail"] = f"Consulta na 001: {bruto['faultstring']}"
        print(f"Erro ao consultar pedido: {bruto['faultstring']}")
        return "erro"

    pedido = bruto.get("pedido_venda_produto", bruto)
    if "cabecalho" not in pedido:
        print("Pedido sem [cabecalho].")
        return "erro"

    cab = pedido["cabecalho"]
    audit["number"] = cab.get("numero_pedido")
    if str(cab.get("etapa", "")).strip() not in ETAPAS_GATILHO:
        audit["detail"] = "Pedido fora da etapa 10/00 na 001."
        return "ignorado_etapa"

    # Confirma a UF pelo cliente do pedido na origem, mesmo sem idCliente no evento.
    id_cliente_origem = cab.get("codigo_cliente")
    if not id_cliente_origem:
        print("Pedido sem codigo do cliente na origem.")
        return "erro"
    cli = consultar_cliente_origem(id_cliente_origem)
    if not cli or cli.get("faultstring"):
        print("Falha ao consultar cliente na origem.")
        return "erro"
    estado = str(cli.get("estado") or "").strip().upper()
    nome = str(cli.get("nome_fantasia") or "").strip() or str(cli.get("razao_social") or "").strip()
    numero_001 = cab.get("numero_pedido") or "(numero indisponivel)"
    identificacao = f"Pedido 001 {numero_001} | Cliente: {nome or '(nome indisponivel)'} | UF: {estado}"
    if estado not in ESTADOS_PERMITIDOS:
        audit["detail"] = "UF fora da lista ES/MG."
        print(f"{identificacao} | Ignorado: UF fora da lista.")
        return "ignorado_estado"

    if not pedido.get("det"):
        audit["detail"] = "Pedido ainda sem itens na 001."
        print(f"{identificacao} | Aguardando itens.")
        return "aguardando_itens"

    cod_int = cab.get("codigo_pedido_integracao") or str(codigo_pedido_origem)
    cod_int_destino = f"{cod_int}-ATIVA"
    try:
        pedido_existente = pedido_existente_na_ativa(cod_int_destino, exigir_resposta=reenvio)
    except ValueError as erro:
        audit["detail"] = str(erro)
        return "erro"
    if pedido_existente:
        numero_004 = pedido_existente.get("numero_pedido")
        destino = f"Pedido 004 {numero_004}" if numero_004 else "Pedido na 004"
        audit.update(destination=numero_004, detail="Pedido ja existente na 004.")
        print(f"{identificacao} | {destino} ja existe.")
        return "ja_existe"

    try:
        mapa = json.loads(os.environ.get("OMIE_DE_PARA_004", "{}"))
        if not isinstance(mapa, dict) or not all(isinstance(grupo, dict) for grupo in mapa.values()):
            raise ValueError("OMIE_DE_PARA_004 precisa conter grupos de codigos origem/destino.")
        preparar_dados_destino(pedido, mapa)
    except (ValueError, TypeError, AttributeError) as erro:
        audit["detail"] = f"Dados de destino: {erro}"
        print(f"{identificacao} | Dados de destino inconsistentes: {erro}")
        return "erro"

    print(f"{identificacao} | Iniciando replicacao para a 004.")

    id_cliente_destino = espelhar_cliente_destino(cli)
    if not id_cliente_destino:
        audit["detail"] = "Nao foi possivel espelhar o cliente na 004."
        print(f"{identificacao} | Nao foi possivel espelhar o cliente.")
        return "erro"

    # 8.4 Ajusta cabecalho
    cab["codigo_cliente"]            = id_cliente_destino
    cab["codigo_pedido_integracao"]  = cod_int_destino
    cab["etapa"]                     = ETAPA_ENTRADA_DESTINO
    cab["origem_pedido"]             = "API"
    cab.pop("codigo_pedido", None)
    cab.pop("numero_pedido", None)

    # 8.5 Itens
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
                audit["detail"] = f"SKU {sku} sem cadastro correspondente."
                print(f"{identificacao} | SKU {sku} nao resolvido na FRI ou na ATIVA.")
                return "erro"
        prod.pop("valor_total", None)

        # Preserva obs_item (contem lote/fabricacao/validade da FRI)
        # para o webhook-rastreabilidade nao cancelar o pedido no destino.
        # O obs_item ja vem no item original e nao e removido.

    # 8.6 Remove blocos calculados
    for chave in ("infoCadastro", "total_pedido"):
        pedido.pop(chave, None)

    # 8.7 Inclui na ATIVA
    res = chamar_omie(OMIE_PEDIDO_URL, "IncluirPedido",
                      APP_KEY_DESTINO, APP_SECRET_DESTINO, pedido)
    if res.get("codigo_pedido") and not res.get("faultstring"):
        numero_004 = res.get("numero_pedido")
        destino = f"Pedido 004 {numero_004}" if numero_004 else "Pedido na 004"
        audit.update(destination=numero_004, detail="Pedido criado na 004.")
        print(f"{identificacao} | {destino} criado.")
        return "transferido"

    audit["detail"] = f"IncluirPedido na 004: {res.get('faultstring') or 'resposta sem confirmacao'}"
    print(f"{identificacao} | Erro na 004: {res.get('faultstring') or 'resposta sem confirmacao'}")
    return "erro"


# ==========================================================
# 9. WEBHOOK
# ==========================================================
@app.route('/webhook', methods=['POST'])
@app.route('/webhook/omie', methods=['POST'])
def receber_webhook():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "payload_invalido"}), 400
    if payload.get('ping'):
        return jsonify({"status": "ok"}), 200

    mensagem = payload.get('event')
    if not isinstance(mensagem, dict):
        return jsonify({"status": "payload_invalido"}), 400

    topic = str(payload.get('topic', '')).strip().lower()
    etapa_atual = str(mensagem.get('etapa', '')).strip()
    eh_inclusao = topic in {'vendaproduto.incluida', 'vendaproduto.incluido'}
    if not eh_inclusao and (
        topic != 'vendaproduto.etapaalterada' or etapa_atual not in ETAPAS_GATILHO
    ):
        return jsonify({"status": "ignorado"}), 200

    try:
        codigo_pedido = int(mensagem.get('idPedido') or mensagem.get('codigo_pedido'))
    except (TypeError, ValueError):
        return jsonify({"status": "pedido_invalido"}), 400
    if codigo_pedido <= 0:
        return jsonify({"status": "pedido_invalido"}), 400

    with _lock:
        if codigo_pedido in _pedidos_em_processamento:
            return jsonify({"status": "em_processamento"}), 200
        ultimo = _pedidos_ultimo_processo.get(codigo_pedido, 0)
        if time.time() - ultimo < COOLDOWN_PEDIDO:
            return jsonify({"status": "cooldown"}), 200
        _pedidos_em_processamento.add(codigo_pedido)

    try:
        if eh_inclusao:
            time.sleep(5)
        print(f"Verificando pedido {codigo_pedido} (etapa {etapa_atual} | {topic})...")
        audit = {}
        status = transferir_pedido_omie(codigo_pedido, audit)
        if status in {"transferido", "ja_existe"}:
            with _lock:
                _pedidos_ultimo_processo[codigo_pedido] = time.time()
    finally:
        with _lock:
            _pedidos_em_processamento.discard(codigo_pedido)

    if status in {"aguardando_itens", "erro", "transferido", "ja_existe"}:
        try:
            bridge("event", {
                **audit, "order_id": str(codigo_pedido),
                "state": "confirmed" if status in {"transferido", "ja_existe"} else "error",
            }, timeout=3)
        except (requests.RequestException, ValueError):
            print("Nao foi possivel publicar a tentativa no portal.")
    if status in {"aguardando_itens", "erro"}:
        return jsonify({"status": status}), 503
    return jsonify({"status": status}), 200


# ==========================================================
# 10. ROTAS AUXILIARES
# ==========================================================
@app.route('/portal/ativa/reenviar', methods=['POST'])
def reenviar_pedido_portal():
    token = os.environ.get("ATIVA_BRIDGE_TOKEN", "").strip()
    enviado = request.headers.get("Authorization", "")
    if not token or not hmac.compare_digest(enviado.encode(), f"Bearer {token}".encode()):
        return jsonify({"detail": "Acesso nao autorizado."}), 401
    body = request.get_json(silent=True)
    codigo = str(body.get("order_id", "")) if isinstance(body, dict) else ""
    if not re.fullmatch(r"[1-9][0-9]{0,17}", codigo):
        return jsonify({"detail": "Pedido invalido."}), 422
    codigo_pedido = int(codigo)
    with _lock:
        if codigo_pedido in _pedidos_em_processamento:
            return jsonify({"detail": "Pedido em processamento pelo webhook."}), 409
        if time.time() - _pedidos_ultimo_processo.get(codigo_pedido, 0) < COOLDOWN_PEDIDO:
            return jsonify({"detail": "Pedido processado recentemente. Aguarde a conferencia."}), 409
        _pedidos_em_processamento.add(codigo_pedido)
    try:
        audit = {}
        status = transferir_pedido_omie(codigo_pedido, audit, reenvio=True)
        if status in {"transferido", "ja_existe"}:
            with _lock:
                _pedidos_ultimo_processo[codigo_pedido] = time.time()
        return jsonify({"status": status, **audit})
    finally:
        with _lock:
            _pedidos_em_processamento.discard(codigo_pedido)


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
