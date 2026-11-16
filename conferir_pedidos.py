import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from app import ESTADOS_PERMITIDOS, ETAPAS_GATILHO, OMIE_CLIENTE_URL, OMIE_PEDIDO_URL
from portal_ativa import bridge


INTERVALO_API = 1.1
SEM_REGISTROS = re.compile(r"(?:ERROR:\s*)?N[aã]o existem registros para a p[aá]gina \[1\][!.]?", re.I)


def consultar_omie(url, metodo, empresa, parametros):
    ambiente = "ORIGEM" if empresa == "001" else "DESTINO"
    contexto = f"Omie {empresa} / {metodo}"
    time.sleep(INTERVALO_API)
    try:
        resposta = requests.post(url, json={
            "call": metodo,
            "app_key": os.environ[f"APP_KEY_{ambiente}"],
            "app_secret": os.environ[f"APP_SECRET_{ambiente}"],
            "param": [parametros],
        }, timeout=60)
    except requests.RequestException:
        raise RuntimeError(f"{contexto}: falha de conexao.") from None
    if resposta.status_code in (425, 429):
        raise RuntimeError(f"{contexto}: limite ou bloqueio HTTP {resposta.status_code}.")

    try:
        dados = resposta.json()
    except ValueError:
        raise RuntimeError(f"{contexto}: resposta invalida (HTTP {resposta.status_code}).") from None
    if not isinstance(dados, dict):
        raise RuntimeError(f"{contexto}: formato de resposta invalido.")
    falha = str(dados.get("faultstring", ""))
    codigo = str(dados.get("faultcode", ""))
    if metodo == "ListarPedidos" and parametros["pagina"] == 1 and SEM_REGISTROS.fullmatch(falha.strip()):
        return {"total_de_paginas": 0, "pedido_venda_produto": []}
    if falha or codigo or not resposta.ok:
        bloqueio = re.search(r"MISUSE_API_PROCESS|REDUNDANT|bloquead[ao]|too many requests", f"{codigo} {falha}", re.I)
        motivo = "limite, bloqueio ou consulta redundante" if bloqueio else "consulta rejeitada"
        raise RuntimeError(f"{contexto}: {motivo} (HTTP {resposta.status_code}).")
    return dados


def listar_pedidos(empresa, inicio, fim):
    pagina = 1
    while True:
        dados = consultar_omie(OMIE_PEDIDO_URL, "ListarPedidos", empresa, {
            "pagina": pagina,
            "registros_por_pagina": 100,
            "apenas_resumo": "N",
            "filtrar_por_data_de": inicio.strftime("%d/%m/%Y"),
            "filtrar_por_data_ate": fim.strftime("%d/%m/%Y"),
        })
        total_paginas = dados.get("total_de_paginas")
        pedidos = dados.get("pedido_venda_produto", [])
        if not str(total_paginas).isdigit() or not isinstance(pedidos, list):
            raise RuntimeError(f"ListarPedidos na {empresa}: paginacao invalida.")
        if not pedidos and int(total_paginas) >= pagina:
            raise RuntimeError(f"ListarPedidos na {empresa}: pagina {pagina} vazia.")
        yield from pedidos
        if pagina >= int(total_paginas):
            return
        pagina += 1


def conferir(inicio, fim, relatorio=None):
    # Carrega todas as paginas do destino antes de classificar pendencias.
    destino = {}
    for pedido in listar_pedidos("004", inicio, fim):
        cabecalho = pedido.get("cabecalho") or {}
        codigo = str(cabecalho.get("codigo_pedido_integracao") or "").strip()
        if codigo.endswith("-ATIVA"):
            destino[codigo] = cabecalho.get("numero_pedido")

    totais = {"confirmado": 0, "pendente": 0, "erro_consulta_001": 0}
    clientes = {}
    vistos = set()
    for pedido in listar_pedidos("001", inicio, fim):
        cabecalho = pedido.get("cabecalho") or {}
        if str(cabecalho.get("etapa", "")).strip() not in ETAPAS_GATILHO:
            continue
        codigo_pedido = cabecalho.get("codigo_pedido")
        codigo_cliente = cabecalho.get("codigo_cliente")
        numero_001 = cabecalho.get("numero_pedido")
        if not codigo_pedido or not codigo_cliente:
            if codigo_pedido and relatorio is not None:
                relatorio.append({"id": str(codigo_pedido), "number": numero_001, "uf": "", "status": "erro_consulta_001"})
            totais["erro_consulta_001"] += 1
            print(json.dumps({"status": "erro_consulta_001", "pedido_001": numero_001}))
            continue
        if codigo_pedido in vistos:
            continue
        vistos.add(codigo_pedido)
        if codigo_cliente not in clientes:
            clientes[codigo_cliente] = consultar_omie(
                OMIE_CLIENTE_URL, "ConsultarCliente", "001", {"codigo_cliente_omie": codigo_cliente},
            )
        cliente = clientes[codigo_cliente]
        uf = str(cliente.get("estado") or "").strip().upper()
        nome = str(cliente.get("nome_fantasia") or "").strip() or str(cliente.get("razao_social") or "").strip()
        numero_004 = None
        if not uf:
            status = "erro_consulta_001"
        elif uf not in ESTADOS_PERMITIDOS:
            continue
        else:
            codigo_base = cabecalho.get("codigo_pedido_integracao") or str(codigo_pedido)
            codigo_destino = f"{codigo_base}-ATIVA".strip()
            status = "confirmado" if codigo_destino in destino else "pendente"
            numero_004 = destino.get(codigo_destino)
        totais[status] += 1
        if relatorio is not None:
            relatorio.append({
                "id": str(codigo_pedido), "number": numero_001, "customer": nome,
                "uf": uf, "destination": numero_004, "status": status,
            })
        print(json.dumps({
            "status": status,
            "pedido_001": numero_001,
            "cliente": nome or None,
            "uf": uf,
            "pedido_004": numero_004,
        }, ensure_ascii=False))
    print(json.dumps({"inicio": inicio.isoformat(), "fim": fim.isoformat(), **totais}))
    return totais


def processar_reenvio(relatorio):
    resposta = bridge("claim", {})
    chamado = resposta.get("claim") if resposta else None
    if not chamado:
        return
    resultado = {"id": chamado["id"], "state": "error"}
    pedido = next((row for row in relatorio if row["id"] == chamado["order_id"]), None)
    if pedido and pedido["status"] == "confirmado":
        resultado.update(state="confirmed", destination=pedido["destination"], detail="Copia ja encontrada na 004.")
    elif not pedido or pedido["status"] != "pendente":
        resultado["detail"] = "Pedido nao confirmado como elegivel e pendente na nova conferencia."
    else:
        try:
            resposta = requests.post(
                "https://replicador-pedidos-ativa.onrender.com/portal/ativa/reenviar",
                json={"order_id": chamado["order_id"]},
                headers={"Authorization": f"Bearer {os.environ['ATIVA_BRIDGE_TOKEN'].strip()}"},
                timeout=240,
            )
            dados = resposta.json()
            if dados.get("status") in {"transferido", "ja_existe"} and resposta.ok:
                resultado.update(state="confirmed", destination=dados.get("destination"))
            resultado["detail"] = dados.get("detail") or "O serviço não confirmou o reenvio."
        except (requests.RequestException, ValueError, AttributeError):
            resultado["detail"] = "Resposta inconclusiva. Confira se a copia existe antes de reenviar."
    bridge("result", resultado)


def main():
    parser = argparse.ArgumentParser(description="Confere pedidos Omie 001 -> 004 em modo somente leitura.")
    parser.add_argument("--dias", type=int, default=7, help="Dias de emissao a consultar, incluindo hoje.")
    args = parser.parse_args()
    if not 1 <= args.dias <= 90:
        parser.error("--dias precisa estar entre 1 e 90.")
    credenciais = ("APP_KEY_ORIGEM", "APP_SECRET_ORIGEM", "APP_KEY_DESTINO", "APP_SECRET_DESTINO")
    ausentes = [nome for nome in credenciais if not os.environ.get(nome, "").strip()]
    if ausentes:
        parser.error(f"Variaveis ausentes: {', '.join(ausentes)}.")
    if not ESTADOS_PERMITIDOS:
        parser.error("ESTADOS_PERMITIDOS precisa conter ES, MG ou ambos.")
    fim = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    relatorio = []
    try:
        totais = conferir(fim - timedelta(days=args.dias - 1), fim, relatorio)
    except RuntimeError as erro:
        print(json.dumps({"status": "conferencia_interrompida", "motivo": str(erro)}))
        try:
            bridge("report", {"error": str(erro)})
        except (requests.RequestException, ValueError):
            print("Falha ao enviar o status da conferencia ao portal.")
        return 1
    try:
        if bridge("report", {"rows": relatorio}):
            processar_reenvio(relatorio)
    except (requests.RequestException, ValueError):
        print("Falha na comunicacao entre a conferencia e o portal.")
        return 1
    return int(any(totais[status] for status in ("pendente", "erro_consulta_001")))


if __name__ == "__main__":
    raise SystemExit(main())
