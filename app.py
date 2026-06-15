import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
# ==========================================
# 1. CREDENCIAIS OMIE (PREENCHA AQUI)
# ==========================================
APP_KEY_ORIGEM = "1724630275368"
APP_SECRET_ORIGEM = "549a26b527f429912abf81f18570030e"

APP_KEY_DESTINO = "5102721230607"
APP_SECRET_DESTINO = "549a26b527f429912abf81f18570030e"

OMIE_API_URL = "https://app.omie.com.br/api/v1/produtos/pedido/"

# ==========================================
# 2. FUNÇÃO DE TRANSFERÊNCIA
# ==========================================
def transferir_pedido_omie(codigo_pedido_origem):
    payload_consulta = {
        "call": "ConsultarPedido",
        "app_key": APP_KEY_ORIGEM,
        "app_secret": APP_SECRET_ORIGEM,
        "param": [{"codigo_pedido": codigo_pedido_origem}]
    }
    
    # Busca na Origem
    pedido_origem_bruto = requests.post(OMIE_API_URL, json=payload_consulta).json()
    print(f"📦 DADOS DA ORIGEM: {pedido_origem_bruto}")
    
    if "faultstring" in pedido_origem_bruto:
        print(f"Erro na origem: {pedido_origem_bruto['faultstring']}")
        return False

    # Desempacotador
    pedido = pedido_origem_bruto.get("pedido_venda_produto", pedido_origem_bruto)

    if "cabecalho" not in pedido:
        print("❌ ERRO INTERNO: O pedido desempacotado não possui a tag [cabecalho].")
        return False

    # Limpeza profunda de IDs exclusivos da origem
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

    # Envio para a ATIVA
    payload_inclusao = {
        "call": "IncluirPedido",
        "app_key": APP_KEY_DESTINO,
        "app_secret": APP_SECRET_DESTINO,
        "param": [pedido]
    }
    
    resultado = requests.post(OMIE_API_URL, json=payload_inclusao).json()
    
    if "codigo_pedido" in resultado:
        print(f"✅ SUCESSO! Pedido transferido para ATIVA. Novo ID: {resultado['codigo_pedido']}")
        return True
    else:
        print(f"❌ ERRO DO OMIE (ATIVA): {resultado}")
        return False

# ==========================================
# 3. ROTA DO WEBHOOK
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
