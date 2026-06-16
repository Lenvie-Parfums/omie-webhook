import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================
# 1. CREDENCIAIS OMIE
#    Use variaveis de ambiente no Render.
#    Os valores apos a virgula sao fallback local.
# ==========================================
APP_KEY_ORIGEM = os.environ.get("APP_KEY_ORIGEM", "1724630275368")
APP_SECRET_ORIGEM = os.environ.get("APP_SECRET_ORIGEM", "549a26b527f429912abf81f18570030e")

APP_KEY_DESTINO = os.environ.get("APP_KEY_DESTINO", "5102721230607")
APP_SECRET_DESTINO = os.environ.get("APP_SECRET_DESTINO", "e3e98a53e601102596075966c6c5f5a1")

OMIE_PEDIDO_URL = "https://app.omie.com.br/api/v1/produtos/pedido/"
OMIE_CLIENTE_URL = "https://app.omie.com.br/api/v1/geral/clientes/"

ETAPA_GATILHO = "80"


# ==========================================
# 2. TRADUTOR DE CLIENTE (ORIGEM -> ATIVA por CNPJ/CPF)
# ==========================================
def obter_cliente_destino(codigo_cliente_origem):
    # --- Pega o CNPJ/CPF na ORIGEM ---
    payload_origem = {
        "call": "ConsultarCliente",
        "app_key": APP_KEY_ORIGEM,
        "app_secret": APP_SECRET_ORIGEM,
        "param": [{"codigo_cliente_omie": codigo_cliente_origem}]
    }
    cli_origem = requests.post(OMIE_CLIENTE_URL, json=payload_origem).json()
    print(f"🔎 RETORNO ConsultarCliente ORIGEM: {cli_origem}")

    cnpj_cpf = cli_origem.get("cnpj_cpf")
    print(f"🔎 CNPJ/CPF EXTRAIDO: {cnpj_cpf}")

    if not cnpj_cpf:
        print("⚠️ CNPJ/CPF veio vazio do ConsultarCliente da origem.")
        return None

    # Normaliza: remove pontuacao para casar com qualquer formato gravado na ATIVA
    cnpj_limpo = "".join(filter(str.isalnum, cnpj_cpf))

    # --- Procura o mesmo CNPJ/CPF na ATIVA ---
    payload_destino = {
        "call": "ListarClientes",
        "app_key": APP_KEY_DESTINO,
        "app_secret": APP_SECRET_DESTINO,
        "param": [{
            "pagina": 1,
            "registros_por_pagina": 10,
            "clientesFiltro": {"cnpj_cpf": cnpj_limpo}
        }]
    }
    busca_destino = requests.post(OMIE_CLIENTE_URL, json=payload_destino).json()
    print(f"🔎 RETORNO ListarClientes ATIVA: {busca_destino}")

    clientes = busca_destino.get("clientes_cadastro", [])
    if clientes:
        id_destino = clientes[0]["codigo_cliente_omie"]
        print(f"✅ Cliente encontrado na ATIVA. ID destino: {id_destino}")
        return id_destino

    print("❌ Cliente nao encontrado na ATIVA com esse CNPJ/CPF.")
    return None


# ==========================================
# 3. CHECAGEM DE IDEMPOTENCIA
#    Evita duplicar pedido se a Omie reenviar o webhook.
# ==========================================
def pedido_ja_existe_na_ativa(codigo_pedido_integracao):
    payload = {
        "call": "ConsultarPedido",
        "app_key": APP_KEY_DESTINO,
        "app_secret": APP_SECRET_DESTINO,
        "param": [{"codigo_pedido_integracao": codigo_pedido_integracao}]
    }
    resp = requests.post(OMIE_PEDIDO_URL, json=payload).json()
    # Se achou o pedido, vem 'pedido_venda_produto'; se nao, vem faultstring
    if "pedido_venda_produto" in resp:
        print(f"♻️ Pedido {codigo_pedido_integracao} JA existe na ATIVA. Ignorando.")
        return True
    return False


# ==========================================
# 4. FUNCAO DE TRANSFERENCIA
# ==========================================
def transferir_pedido_omie(codigo_pedido_origem):
    # --- 1. Consulta na ORIGEM ---
    payload_consulta = {
        "call": "ConsultarPedido",
        "app_key": APP_KEY_ORIGEM,
        "app_secret": APP_SECRET_ORIGEM,
        "param": [{"codigo_pedido": codigo_pedido_origem}]
    }
    pedido_origem_bruto = requests.post(OMIE_PEDIDO_URL, json=payload_consulta).json()

    if "faultstring" in pedido_origem_bruto:
        print(f"❌ Erro na origem: {pedido_origem_bruto['faultstring']}")
        return False

    pedido = pedido_origem_bruto.get("pedido_venda_produto", pedido_origem_bruto)

    if "cabecalho" not in pedido:
        print("❌ ERRO: pedido desempacotado nao possui [cabecalho].")
        return False

    # --- 2. Idempotencia: monta o codigo de integracao e checa se ja existe ---
    cod_int = pedido["cabecalho"].get("codigo_pedido_integracao", str(codigo_pedido_origem))
    codigo_integracao_destino = f"{cod_int}-ATIVA"

    if pedido_ja_existe_na_ativa(codigo_integracao_destino):
        return True  # ja foi transferido antes; trata como sucesso

    # --- 3. Traduzir o CLIENTE (origem -> ATIVA) ---
    id_origem = pedido["cabecalho"].get("codigo_cliente")
    print(f"🔍 Buscando CNPJ do cliente origem {id_origem}...")

    id_destino = obter_cliente_destino(id_origem)
    if not id_destino:
        print("❌ ERRO: Cliente nao encontrado na ATIVA (CNPJ nao cadastrado).")
        return False

    pedido["cabecalho"]["codigo_cliente"] = id_destino
    print(f"✅ Cliente traduzido para o ID {id_destino} da ATIVA.")

    # --- 4. Limpeza de IDs internos da ORIGEM ---
    pedido["cabecalho"].pop("codigo_pedido", None)
    pedido["cabecalho"].pop("numero_pedido", None)
    pedido["cabecalho"].pop("codigo_cenario_impostos", None)
    pedido["cabecalho"]["codigo_pedido_integracao"] = codigo_integracao_destino

    if "informacoes_adicionais" in pedido:
        pedido["informacoes_adicionais"].pop("codigo_conta_corrente", None)

    # --- 5. Limpeza por item (det) ---
    #    Produto: removemos o codigo_produto numerico da origem e
    #    mandamos pelo codigo (SKU), que e igual nos dois CNPJs.
    if "det" in pedido:
        for item in pedido["det"]:
            ide = item.get("ide", {})
            ide.pop("codigo_item_pedido", None)

            prod = item.get("produto", {})
            # Garante que a ATIVA resolva o produto pelo SKU, nao pelo ID interno
            if prod.get("codigo"):
                prod.pop("codigo_produto", None)
            # Remove valores calculados que a Omie rejeita na inclusao
            prod.pop("valor_total", None)

            inf_adic = item.get("inf_adic", {})
            inf_adic.pop("codigo_local_estoque", None)
            inf_adic.pop("codigo_cenario_impostos_item", None)

    # --- 6. Remove blocos read-only / calculados do pedido ---
    for chave in ["infoCadastro", "departamentos", "observacoes", "total_pedido"]:
        pedido.pop(chave, None)

    # --- 7. Inclusao na ATIVA ---
    payload_inclusao = {
        "call": "IncluirPedido",
        "app_key": APP_KEY_DESTINO,
        "app_secret": APP_SECRET_DESTINO,
        "param": [pedido]
    }
    resultado = requests.post(OMIE_PEDIDO_URL, json=payload_inclusao).json()

    if "codigo_pedido" in resultado:
        print(f"✅ SUCESSO! Pedido transferido. Novo ID ATIVA: {resultado['codigo_pedido']}")
        return True
    else:
        print(f"❌ ERRO DO OMIE (ATIVA): {resultado}")
        return False


# ==========================================
# 5. ROTA DO WEBHOOK
# ==========================================
@app.route('/webhook/omie', methods=['POST'])
def receber_webhook():
    payload = request.json

    # Ping de validacao do Omie
    if payload and payload.get('ping'):
        return jsonify({"status": "ok"}), 200

    mensagem = payload.get('event', {}) if payload else {}
    codigo_pedido = mensagem.get('idPedido')
    etapa_atual = str(mensagem.get('etapa', ''))

    if etapa_atual == ETAPA_GATILHO:
        print(f"⏳ Tentando transferir pedido {codigo_pedido} (etapa {etapa_atual})...")
        sucesso = transferir_pedido_omie(codigo_pedido)
        # SEMPRE 200: se devolver 500, a Omie reenfileira o MESMO pedido
        # infinitamente e trava a fila para os pedidos seguintes.
        return jsonify({"status": "transferido" if sucesso else "erro"}), 200

    return jsonify({"status": "ignorado"}), 200


# Healthcheck simples para o Render nao dar 404 na raiz
@app.route('/', methods=['GET', 'HEAD'])
def home():
    return jsonify({"status": "online"}), 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
