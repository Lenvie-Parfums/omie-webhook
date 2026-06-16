import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================================
# 1. CREDENCIAIS OMIE  (use variaveis de ambiente no Render)
# ==========================================================
APP_KEY_ORIGEM = os.environ.get("APP_KEY_ORIGEM", "1724630275368")
APP_SECRET_ORIGEM = os.environ.get("APP_SECRET_ORIGEM", "549a26b527f429912abf81f18570030e")

APP_KEY_DESTINO = os.environ.get("APP_KEY_DESTINO", "5102721230607")
APP_SECRET_DESTINO = os.environ.get("APP_SECRET_DESTINO", "e3e98a53e601102596075966c6c5f5a1")

OMIE_PEDIDO_URL = "https://app.omie.com.br/api/v1/produtos/pedido/"
OMIE_CLIENTE_URL = "https://app.omie.com.br/api/v1/geral/clientes/"

ETAPA_GATILHO = "80"
# Etapa em que o pedido ENTRA na ATIVA. A 80 e do fluxo da FRI; no destino
# o pedido deve entrar numa etapa inicial valida. Ajuste se necessario.
ETAPA_ENTRADA_DESTINO = "10"
# Categoria (plano de contas) usada quando o pedido da FRI nao traz uma.
# 1.01.02 = "Venda Atacado - Representantes" (fluxo dos representantes).
CATEGORIA_PADRAO = os.environ.get("CATEGORIA_PADRAO", "1.01.02")


# ==========================================================
# HELPER GENERICO DE CHAMADA OMIE
# ==========================================================
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
    return resp


# ==========================================================
# 2. ESPELHAR O CLIENTE (ORIGEM -> ATIVA)
#    Puxa o cadastro completo na FRI e recria/atualiza na ATIVA
#    via UpsertCliente (chave = codigo_cliente_integracao).
#    Retorna o codigo_cliente_omie do DESTINO.
# ==========================================================
def espelhar_cliente_destino(codigo_cliente_origem):
    # --- 2.1 Cadastro completo na ORIGEM ---
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

    # Codigo de integracao deterministico = derivado do CNPJ.
    # Garante idempotencia: o mesmo cliente sempre cai no mesmo registro.
    cnpj_limpo = "".join(filter(str.isalnum, cnpj_cpf))
    cod_int_cliente = f"FRI-{cnpj_limpo}"

    # --- 2.2 Monta o cadastro para o DESTINO, copiando os campos fiscais ---
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

    # Trava da Omie: nao fatura cliente cujo nome comece com "Cliente".
    if str(upsert.get("razao_social", "")).strip().lower().startswith("cliente"):
        print("AVISO: razao social comeca com 'Cliente' - Omie bloqueia faturamento.")

    # --- 2.3 Upsert no DESTINO (cria se nao existe, atualiza se existe) ---
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
    # --- 4.1 Consulta na ORIGEM ---
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

    # --- 4.2 Idempotencia ---
    cod_int = pedido["cabecalho"].get("codigo_pedido_integracao") or str(codigo_pedido_origem)
    codigo_integracao_destino = f"{cod_int}-ATIVA"
    if pedido_ja_existe_na_ativa(codigo_integracao_destino):
        return True

    # --- 4.3 Espelhar o cliente (cria/atualiza na ATIVA com dados fiscais) ---
    id_origem_cliente = pedido["cabecalho"].get("codigo_cliente")
    print(f"Espelhando cliente origem {id_origem_cliente}...")
    id_destino_cliente = espelhar_cliente_destino(id_origem_cliente)
    if not id_destino_cliente:
        print("Nao foi possivel espelhar o cliente na ATIVA.")
        return False
    pedido["cabecalho"]["codigo_cliente"] = id_destino_cliente

    # --- 4.4 Limpeza de IDs internos da ORIGEM no cabecalho ---
    cab = pedido["cabecalho"]
    cab.pop("codigo_pedido", None)
    cab.pop("numero_pedido", None)
    cab.pop("codigo_cenario_impostos", None)
    cab["codigo_pedido_integracao"] = codigo_integracao_destino
    # A etapa "80" e do fluxo da FRI e nao existe como entrada na ATIVA.
    # Entra sempre na etapa inicial padrao do destino (10 = registrado).
    cab["etapa"] = ETAPA_ENTRADA_DESTINO
    # origem_pedido "ERP" da FRI nao e aceita na ATIVA. Como entra via API,
    # forcamos "API", que esta na lista de origens validas do destino.
    cab["origem_pedido"] = "API"
    # Transportadora tem ID interno na FRI que nao existe na ATIVA.
    # Removida no teste; definir depois no destino se necessario.
    cab.pop("codigo_transportadora", None)

    if "informacoes_adicionais" in pedido and isinstance(pedido["informacoes_adicionais"], dict):
        pedido["informacoes_adicionais"].pop("codigo_conta_corrente", None)
        # Forca a categoria correta da ATIVA. O plano de contas da FRI pode ter
        # codigos iguais com significado diferente, entao nao herdamos da origem.
        # Todos esses pedidos sao de representantes -> 1.01.02.
        pedido["informacoes_adicionais"]["codigo_categoria"] = CATEGORIA_PADRAO
    else:
        pedido["informacoes_adicionais"] = {"codigo_categoria": CATEGORIA_PADRAO}

    # Transportadora tambem pode vir aninhada no bloco frete.
    if "frete" in pedido and isinstance(pedido["frete"], dict):
        pedido["frete"].pop("codigo_transportadora", None)

    # --- 4.5 Limpeza por item (preserva SKU e descricao p/ o destino resolver) ---
    if "det" in pedido and isinstance(pedido["det"], list):
        for item in pedido["det"]:
            ide = item.get("ide", {})
            ide.pop("codigo_item_pedido", None)

            prod = item.get("produto", {})
            if prod.get("codigo") or prod.get("descricao"):
                prod.pop("codigo_produto", None)
            prod.pop("valor_total", None)

            inf = item.get("inf_adic", {})
            inf.pop("codigo_local_estoque", None)
            inf.pop("codigo_cenario_impostos_item", None)

    # --- 4.6 Remove blocos read-only / calculados ---
    for chave in ["infoCadastro", "total_pedido", "departamentos"]:
        pedido.pop(chave, None)

    # --- 4.7 Inclusao na ATIVA ---
    res = chamar_omie(
        OMIE_PEDIDO_URL, "IncluirPedido",
        APP_KEY_DESTINO, APP_SECRET_DESTINO, pedido
    )

    if "codigo_pedido" in res:
        print(f"SUCESSO! Pedido na ATIVA. Novo ID: {res['codigo_pedido']}")
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
        print(f"Transferindo pedido {codigo_pedido} (etapa {etapa_atual})...")
        sucesso = transferir_pedido_omie(codigo_pedido)
        # SEMPRE 200: 500 faz a Omie reenfileirar o mesmo evento e travar a fila.
        return jsonify({"status": "transferido" if sucesso else "erro"}), 200

    return jsonify({"status": "ignorado"}), 200


@app.route('/', methods=['GET', 'HEAD'])
def home():
    return jsonify({"status": "online"}), 200


@app.route('/categorias', methods=['GET'])
def listar_categorias():
    # Lista as categorias (plano de contas) da ATIVA para voce escolher
    # o codigo de venda correto e colocar em CATEGORIA_PADRAO.
    url = "https://app.omie.com.br/api/v1/geral/categorias/"
    resp = chamar_omie(
        url, "ListarCategorias",
        APP_KEY_DESTINO, APP_SECRET_DESTINO,
        {"pagina": 1, "registros_por_pagina": 500}
    )
    cats = resp.get("categoria_cadastro", [])
    # Devolve so o essencial: codigo e descricao
    enxuto = [
        {"codigo": c.get("codigo"), "descricao": c.get("descricao")}
        for c in cats
    ]
    return jsonify({"total": len(enxuto), "categorias": enxuto}), 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
