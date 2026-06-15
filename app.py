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

OMIE_API_URL = "https://app.omie.com.br/api/v1/produtos/pedido/"

# ==========================================
# 2. FUNÇÃO DE TRANSFERÊNCIA
# ==========================================
def transferir_pedido_omie(codigo_pedido_origem):
    # Consulta na Origem
    payload_consulta = {
        "call": "ConsultarPedido",
        "app_key": APP_KEY_ORIGEM,
        "app_secret": APP_SECRET_ORIGEM,
        "param": [{"codigo_pedido": codigo_pedido_origem}]
    }
    
    req_origem = requests.post(OMIE_API_URL, json=payload_consulta)
    pedido = req_origem.json()
    
    if "faultstring" in pedido:
        return False

    # Limpeza dos IDs de controle (Origem -> Destino)
    if "cabecalho" in pedido:
        pedido["cabecalho"].pop("codigo_pedido", None)
        cod_int = pedido["cabecalho"].get("codigo_pedido_integracao", str(codigo_pedido_origem))
        pedido["cabecalho"]["codigo_pedido_integracao"] = f"{cod_int}-ATIVA"
        
    if "det" in pedido:
        for item in pedido["det"]:
            if "ide" in item:
                item["ide"].pop("codigo_item_pedido", None)
                
    for chave in ["infoCadastro", "departamentos"]:
        pedido.pop(chave, None)

    # Inclusão no Destino (ATIVA)
    payload_inclusao = {
        "call": "IncluirPedido",
        "app_key": APP_KEY_DESTINO,
        "app_secret": APP_SECRET_DESTINO,
        "param": [pedido]
    }
    
    req_destino = requests.post(OMIE_API_URL, json=payload_inclusao)
    resultado = req_destino.json()
    
    return "codigo_pedido" in resultado

# ==========================================
# 3. ROTA DO WEBHOOK (GATILHO)
# ==========================================
@app.route('/webhook/omie', methods=['POST'])
def receber_webhook():
    payload = request.json
    
    # Resposta de validação inicial do Omie
    if payload and payload.get('ping'):
        return jsonify({"status": "ok"}), 200

    mensagem = payload.get('event', {})
    codigo_pedido = mensagem.get('idPedido')
    etapa_atual = str(mensagem.get('etapa', ''))

    # Trava: Só executa se for etapa 80
    if etapa_atual == "80":
        sucesso = transferir_pedido_omie(codigo_pedido)
        if sucesso:
            return jsonify({"status": "transferido"}), 200
        else:
            return jsonify({"status": "erro"}), 500
            
    # Ignora outras etapas
    return jsonify({"status": "ignorado"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)