import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import (
    ESTADOS_PERMITIDOS,
    ETAPAS_GATILHO,
    OMIE_PEDIDO_URL,
    chamar_omie,
    consultar_cliente_origem,
)


def consultar_pedidos_origem(inicio, fim):
    pagina = 1
    while True:
        resposta = chamar_omie(
            OMIE_PEDIDO_URL, "ListarPedidos",
            os.environ["APP_KEY_ORIGEM"], os.environ["APP_SECRET_ORIGEM"],
            {
                "pagina": pagina,
                "registros_por_pagina": 100,
                "apenas_resumo": "N",
                "filtrar_por_data_de": inicio.strftime("%d/%m/%Y"),
                "filtrar_por_data_ate": fim.strftime("%d/%m/%Y"),
            },
        )
        if resposta.get("faultstring") or "total_de_paginas" not in resposta:
            raise RuntimeError(f"ListarPedidos na 001 falhou na pagina {pagina}.")
        yield from resposta.get("pedido_venda_produto", [])
        if pagina >= int(resposta["total_de_paginas"]):
            break
        pagina += 1
        time.sleep(0.5)


def conferir_destino(codigo_integracao):
    resposta = chamar_omie(
        OMIE_PEDIDO_URL, "ConsultarPedido",
        os.environ["APP_KEY_DESTINO"], os.environ["APP_SECRET_DESTINO"],
        {"codigo_pedido_integracao": codigo_integracao},
    )
    pedido = resposta.get("pedido_venda_produto", resposta)
    cabecalho = pedido.get("cabecalho") or {}
    if cabecalho.get("codigo_pedido") and cabecalho.get("codigo_pedido_integracao") == codigo_integracao:
        return "confirmado", cabecalho.get("numero_pedido") or cabecalho["codigo_pedido"]

    # Distingue uma ausencia confirmada de falhas de rede, autenticacao e limites.
    falha = str(resposta.get("faultstring", ""))
    if re.search(r"\bpedido\b.*\bn[ãa]o (?:encontrad[oa]|cadastrad[oa]|existe|localizad[oa])", falha, re.I):
        return "pendente", None
    return "erro_consulta_004", None


def conferir(inicio, fim):
    totais = {"confirmado": 0, "pendente": 0, "erro_consulta_001": 0, "erro_consulta_004": 0}
    vistos = set()
    for pedido in consultar_pedidos_origem(inicio, fim):
        cabecalho = pedido.get("cabecalho") or {}
        if str(cabecalho.get("etapa", "")).strip() not in ETAPAS_GATILHO:
            continue
        pedido_001 = cabecalho.get("codigo_pedido")
        if not pedido_001 or not cabecalho.get("codigo_cliente"):
            totais["erro_consulta_001"] += 1
            print(json.dumps({"status": "erro_consulta_001", "pedido_001": pedido_001}))
            continue
        if pedido_001 in vistos:
            continue
        vistos.add(pedido_001)
        cliente = consultar_cliente_origem(cabecalho["codigo_cliente"])
        uf = str(cliente.get("estado", "")).strip().upper()
        if not uf:
            status, pedido_004 = "erro_consulta_001", None
        elif uf not in ESTADOS_PERMITIDOS:
            continue
        else:
            codigo_base = cabecalho.get("codigo_pedido_integracao") or str(pedido_001)
            status, pedido_004 = conferir_destino(f"{codigo_base}-ATIVA")
            time.sleep(0.5)
        totais[status] += 1
        print(json.dumps({"status": status, "pedido_001": pedido_001, "pedido_004": pedido_004, "uf": uf}))
    print(json.dumps({"inicio": inicio.isoformat(), "fim": fim.isoformat(), **totais}))
    return totais


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
    totais = conferir(fim - timedelta(days=args.dias - 1), fim)
    return int(any(totais[status] for status in ("pendente", "erro_consulta_001", "erro_consulta_004")))


if __name__ == "__main__":
    raise SystemExit(main())
