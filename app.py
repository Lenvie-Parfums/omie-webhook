

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================
# 1. CREDENCIAIS OMIE (PREENCHA AQUI)
# ==========================================
APP_KEY_ORIGEM = "1724630275368"
APP_SECRET_ORIGEM = "549a26b527f429912abf81f18570030e"

APP_KEY_DESTINO = "5102721230607"
APP_SECRET_DESTINO = "e3e98a53e601102596075966c6c5f5a1"

OMIE_PEDIDO_URL = "https://app.omie.com.br/api/v1/produtos/pedido/"
OMIE_CLIENTE_URL = "https://app.omie.com.br/api/v1/geral/clientes/"

# ==========================================
# 2. TRADUTOR DE CLIENTE (ORIGEM -> ATIVA)
# ==========================================
def obter_cliente_destino(codigo_cliente_origem):
    # Pega o CNPJ na Origem
    payload_origem = {
        "call": "ConsultarCliente",
        "app_key": APP_KEY_ORIGEM,
        "app_secret": APP_SECRET_ORIGEM,
        "param": [{"codigo_cliente_omie": codigo_cliente_origem}]
    }
    cli_origem = requests.post(OMIE_CLIENTE_URL, json=payload_origem).json()
    cnpj_cpf = cli_origem.get("cnpj_cpf")
    
    if not cnpj_cpf:
        return None
        
    # Procura o mesmo CNPJ na ATIVA
    payload_destino = {
        "call": "ListarClientes",
        "app_key": APP_KEY_DESTINO,
        "app_secret": APP_SECRET_DESTINO,
        "param": [{"pagina": 1, "registros_por_pagina": 10, "clientesFiltro": {"cnpj_cpf": cnpj_cpf}}]
    }
    busca_destino = requests.post(OMIE_CLIENTE_URL, json=payload_destino).json()
    
    clientes = busca_destino.get("clientes_cadastro", [])
    if clientes:
        return clientes[0]["codigo_cliente_omie"] # Retorna o ID correto da ATIVA
        
    return None

# ==========================================
# 3. FUNÇÃO DE TRANSFERÊNCIA
# ==========================================
def transferir_pedido_omie(codigo_pedido_origem):
    payload_consulta = {
        "call": "ConsultarPedido",
        "app_key": APP_KEY_ORIGEM,
        "app_secret": APP_SECRET_ORIGEM,
        "param": [{"codigo_pedido": codigo_pedido_origem}]
    }
    
    pedido_origem_bruto = requests.post(OMIE_PEDIDO_URL, json=payload_consulta).json()
    
    if "faultstring" in pedido_origem_bruto:
        print(f"Erro na origem: {pedido_origem_bruto['faultstring']}")
        return False

    pedido = pedido_origem_bruto.get("pedido_venda_produto", pedido_origem_bruto)

    if "cabecalho" not in pedido:
        return False
        
    # --- ETAPA NOVA: TRADUZIR O CLIENTE ---
    id_origem = pedido["cabecalho"].get("codigo_cliente")
    print(f"🔍 Buscando CNPJ do cliente {id_origem}...")
    
    id_destino = obter_cliente_destino(id_origem)
    if not id_destino:
        print("❌ ERRO: Cliente não encontrado na ATIVA (CNPJ não cadastrado).")
        return False
        
    pedido["cabecalho"]["codigo_cliente"] = id_destino
    print(f"✅ Cliente traduzido para o ID {id_destino} da ATIVA.")

    # --- LIMPEZA DE IDs ---
    pedido["cabecalho"].pop("codigo_pedido", None)
    pedido["cabecalho"].pop("codigo_cenario_impostos", None)
    
    cod_int = pedido["cabecalho"].get("codigo_pedido_integracao", str(codigo_pedido_origem))
    pedido["cabecalho"]["codigo_pedido_integracao"] = f"{cod_int}-ATIVA"
    
    if "informacoes_adicionais" in pedido:
        pedido["informacoes_adicionais"].pop("codigo_conta_corrente", None)
        
    if "det" in pedido:
        for item in pedido["det"]:
            item.get("ide", {}).pop("codigo_item_pedido", None)
            item.get("inf_adic", {}).pop("codigo_local_estoque", None)
            item.get("inf_adic", {}).pop("codigo_cenario_impostos_item", None)
            
    for chave in ["infoCadastro", "departamentos", "observacoes", "total_pedido"]:
        pedido.pop(chave, None)

    # --- INJEÇÃO NA ATIVA ---
    payload_inclusao = {
        "call": "IncluirPedido",
        "app_key": APP_KEY_DESTINO,
        "app_secret": APP_SECRET_DESTINO,
        "param": [pedido]
    }
    
    resultado = requests.post(OMIE_PEDIDO_URL, json=payload_inclusao).json()
    
    if "codigo_pedido" in resultado:
        print(f"✅ SUCESSO! Pedido transferido. Novo ID: {resultado['codigo_pedido']}")
        return True
    else:
        print(f"❌ ERRO DO OMIE (ATIVA): {resultado}")
        return False

# ==========================================
# 4. ROTA DO WEBHOOK
# ==========================================
@app.route('/webhook/omie', methods=['POST'])
def receber_webhook():
    payload = request.json
    if payload and payload.get('ping'):
        return jsonify({"status": "ok"}), 200

    mensagem = payload.get('event', {})
    codigo_pedido = mensagem.get('idPedido')
    etapa_atual = str(mensagem.get('etapa', ''))

    if etapa_atual == "80":
        print(f"⏳ Tentando transferir pedido {codigo_pedido}...")
        sucesso = transferir_pedido_omie(codigo_pedido)
        return jsonify({"status": "transferido" if sucesso else "erro"}), 200 if sucesso else 500
        
    return jsonify({"status": "ignorado"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
