import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

APP_KEY_ORIGEM = "SUA_CHAVE_ORIGEM"
APP_SECRET_ORIGEM = "SEU_SECRET_ORIGEM"
APP_KEY_DESTINO = "SUA_CHAVE_ATIVA"
APP_SECRET_DESTINO = "SEU_SECRET_ATIVA"

OMIE_API_URL = "https://app.omie.com.br/api/v1/produtos/pedido/"

def transferir_pedido_omie(codigo_pedido_origem):
    payload_consulta = {
        "call": "ConsultarPedido",
        "app_key": APP_KEY_ORIGEM,
        "app_secret": APP_SECRET_ORIGEM,
        "param": [{"codigo_pedido": codigo_pedido_origem}]
    }
    pedido = requests.post(OMIE_API_URL, json=payload_consulta).json()
    
    if "faultstring" in pedido:
        print(f"Erro na origem: {pedido['faultstring']}")
        return False

    if "cabecalho" in pedido:
        pedido["cabecalho"].pop("codigo_pedido", None)
        cod_int = pedido["cabecalho"].get("codigo_pedido_integracao", str(codigo_pedido_origem))
        pedido["cabecalho"]["codigo_pedido_integracao"] = f"{cod_int}-ATIVA"
        
    if "det" in pedido:
        for item in pedido["det"]:
            item.get("ide", {}).pop("codigo_item_pedido", None)
            
    for chave in ["infoCadastro", "departamentos"]:
        pedido.pop(chave, None)

    payload_inclusao = {
        "call": "IncluirPedido",
        "app_key": APP_KEY_DESTINO,
        "app_secret": APP_SECRET_DESTINO,
        "param": [pedido]
    }
    resultado = requests.post(OMIE_API_URL, json=payload_inclusao).json()
    
    if "codigo_pedido" in resultado:
        print(f"✅ SUCESSO! Pedido transferido. Novo ID: {resultado['codigo_pedido']}")
        return True
    else:
        print(f"❌ ERRO DO OMIE (ATIVA): {resultado}")
        return False

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
