import os
os.environ["ATIVA_QUEUE_ENABLED"] = "0"
import unittest
from unittest.mock import patch
import app

ORIGEM = {"razao_social": "TRANSPORTADORA TESTE LTDA", "cnpj_cpf": "11.222.333/0007-01"}


def cliente(codigo, cnpj, inativo="N", tags=()):
    return {"codigo_cliente_omie": codigo, "cnpj_cpf": cnpj, "inativo": inativo, "tags": [{"tag": t} for t in tags]}


def omie(cadastros_004, exato=None, paginas=1):
    """Simula a Omie: consulta da 001, busca exata e listagem paginada da 004."""
    chamadas = []
    def chamar(url, chamada, key, secret, param):
        chamadas.append((chamada, param))
        if chamada == "ConsultarCliente":
            return ORIGEM
        if "clientesFiltro" in param:
            return {"clientes_cadastro": exato or [], "total_de_paginas": 1} if exato else {"faultstring": "ERROR: Nao existem registros para a pagina [1]!"}
        return {"clientes_cadastro": cadastros_004 if param["pagina"] == 1 else [], "total_de_paginas": paginas}
    return chamar, chamadas


class TransportadoraPelaRaizTests(unittest.TestCase):
    def setUp(self):
        app._cache_referencias.clear()
        app._cache_clientes_destino.update(lista=None, validade=0.0)

    def resolver(self, cadastros_004, **kw):
        chamar, chamadas = omie(cadastros_004, **kw)
        with patch.object(app, "chamar_omie", side_effect=chamar):
            return app.resolver_referencia("transportadoras", 99), chamadas

    def test_cnpj_exato_ativo_continua_prioritario(self):
        (codigo, _), chamadas = self.resolver([cliente(2, "11.222.333/0036-46", tags=["Transportadora"])],
                                             exato=[cliente(1, "11.222.333/0007-01")])
        self.assertEqual(codigo, 1)
        self.assertFalse(any("clientesFiltro" not in p and c == "ListarClientes" for c, p in chamadas))

    def test_filial_inativa_usa_unico_cadastro_ativo_da_mesma_raiz(self):
        (codigo, nome), _ = self.resolver([
            cliente(5, "11.222.333/0007-01", inativo="S", tags=["Transportadora"]),
            cliente(6, "11.222.333/0036-46", tags=["Transportadora"]),
            cliente(7, "99.888.777/0001-00", tags=["Transportadora"]),
        ])
        self.assertEqual((codigo, nome), (6, "TRANSPORTADORA TESTE LTDA"))

    def test_varios_ativos_da_mesma_raiz_prefere_tag_transportadora(self):
        (codigo, _), _ = self.resolver([cliente(6, "11.222.333/0036-46", tags=["Transportadora"]),
                                        cliente(8, "11.222.333/0001-90", tags=["Fornecedor"])])
        self.assertEqual(codigo, 6)

    def test_ambiguidade_nao_escolhe(self):
        (codigo, _), _ = self.resolver([cliente(6, "11.222.333/0036-46", tags=["Transportadora"]),
                                        cliente(8, "11.222.333/0001-90", tags=["Transportadora"])])
        self.assertIsNone(codigo)

    def test_sem_cadastro_da_empresa_nao_escolhe(self):
        (codigo, _), _ = self.resolver([cliente(7, "99.888.777/0001-00", tags=["Transportadora"])])
        self.assertIsNone(codigo)

    def test_falha_na_listagem_nao_bloqueia_o_pedido(self):
        def chamar(url, chamada, key, secret, param):
            if chamada == "ConsultarCliente":
                return ORIGEM
            if "clientesFiltro" in param:
                return {"faultstring": "ERROR: Nao existem registros para a pagina [1]!"}
            return {"faultstring": "ERROR: Consumo redundante detectado"}
        with patch.object(app, "chamar_omie", side_effect=chamar):
            self.assertIsNone(app.resolver_referencia("transportadoras", 99)[0])
        self.assertIsNone(app._cache_clientes_destino["lista"])

    def test_listagem_acima_do_limite_nao_escolhe(self):
        with patch.object(app, "LIMITE_PAGINAS_CLIENTES", 2):
            (codigo, _), chamadas = self.resolver([cliente(6, "11.222.333/0036-46", tags=["Transportadora"])], paginas=5)
        self.assertIsNone(codigo)
        self.assertEqual(sum(1 for c, p in chamadas if c == "ListarClientes" and "clientesFiltro" not in p), 2)

    def test_listagem_fica_em_cache(self):
        cadastros = [cliente(6, "11.222.333/0036-46", tags=["Transportadora"])]
        chamar, chamadas = omie(cadastros)
        with patch.object(app, "chamar_omie", side_effect=chamar):
            app.resolver_referencia("transportadoras", 99)
            app.resolver_referencia("transportadoras", 100)
        self.assertEqual(sum(1 for c, p in chamadas if c == "ListarClientes" and "clientesFiltro" not in p), 1)

    def test_cpf_nao_usa_raiz(self):
        def chamar(url, chamada, key, secret, param):
            if chamada == "ConsultarCliente":
                return {"razao_social": "PESSOA", "cnpj_cpf": "123.456.789-01"}
            return {"faultstring": "ERROR: Nao existem registros para a pagina [1]!"}
        with patch.object(app, "chamar_omie", side_effect=chamar) as call:
            self.assertIsNone(app.resolver_referencia("transportadoras", 99)[0])
        self.assertEqual(call.call_count, 2)

    def test_pedido_recebe_transportadora_e_sem_observacao(self):
        pedido = {"cabecalho": {"codigo_parcela": "000", "qtde_parcelas": 1}, "frete": {"codigo_transportadora": 99}}
        chamar, _ = omie([cliente(6, "11.222.333/0036-46", tags=["Transportadora"])])
        with patch.object(app, "chamar_omie", side_effect=chamar):
            app.preparar_dados_destino(pedido, {})
        self.assertEqual(pedido["frete"]["codigo_transportadora"], 6)
        self.assertNotIn("Transportadora na 001", str(pedido.get("observacoes")))


if __name__ == "__main__":
    unittest.main()
