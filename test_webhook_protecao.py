import os
os.environ["ATIVA_QUEUE_ENABLED"] = "0"
import unittest
from unittest.mock import Mock, patch
import app

EVENTO = {"topic": "VendaProduto.Incluida", "event": {"idPedido": 9250803746, "etapa": "00"}}


class ProtecaoWebhookTests(unittest.TestCase):
    def setUp(self):
        app._omie_bloqueio_ate = 0.0
        app._omie_read_calls.clear()
        app._pedidos_ultimo_processo.clear()

    def tearDown(self):
        app._omie_bloqueio_ate = 0.0

    def post(self, payload):
        with patch.object(app.time, "sleep"), patch.object(app, "bridge") as bridge:
            with app.app.test_client() as client:
                return client.post("/webhook/omie", json=payload), bridge

    def test_pedido_inexistente_responde_200_para_omie_nao_reenviar(self):
        with patch.object(app, "chamar_omie", return_value={"faultstring": "ERROR: Pedido não cadastrado para o Código [9250803746] !"}) as call:
            resposta, bridge = self.post(EVENTO)
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.get_json()["status"], "inexistente")
        call.assert_called_once()
        bridge.assert_not_called()

    def test_erro_transitorio_continua_pedindo_reenvio(self):
        with patch.object(app, "chamar_omie", return_value={"faultstring": "Read timed out"}):
            resposta, _ = self.post(EVENTO)
        self.assertEqual(resposta.status_code, 503)

    def test_evento_de_outro_aplicativo_nao_consulta_a_omie(self):
        with patch.object(app, "chamar_omie") as call:
            resposta, _ = self.post({**EVENTO, "appKey": "outro-aplicativo"})
        self.assertEqual(resposta.get_json()["status"], "ignorado_outra_empresa")
        call.assert_not_called()

    def test_evento_da_001_continua_processado(self):
        with patch.object(app, "chamar_omie", return_value={"faultstring": "ERROR: Pedido não cadastrado para o Código [1] !"}) as call:
            self.post({**EVENTO, "appKey": app.APP_KEY_ORIGEM})
        call.assert_called_once()

    def test_bloqueio_suspende_chamadas_ate_o_prazo(self):
        clock = [1000.0]
        bloqueio = Mock(json=Mock(return_value={"faultstring": "ERROR: API bloqueada por consumo indevido. Tente novamente em 1202 segundos."}))
        with patch.object(app.time, "monotonic", side_effect=lambda: clock[0]), patch.object(app.time, "sleep"), \
             patch.object(app.requests, "post", return_value=bloqueio) as post:
            app.chamar_omie("https://api.example.test", "ConsultarPedido", "k", "s", {"codigo_pedido": 1})
            clock[0] += 600
            resposta = app.chamar_omie("https://api.example.test", "ConsultarPedido", "k", "s", {"codigo_pedido": 2})
            self.assertEqual(post.call_count, 1)
            self.assertIn("pausa local", resposta["faultstring"])
            with app.app.test_client() as client:
                self.assertEqual(client.post("/webhook/omie", json=EVENTO).status_code, 503)
            self.assertEqual(post.call_count, 1)
            clock[0] += 700
            post.return_value = Mock(json=Mock(return_value={"ok": True}))
            app.chamar_omie("https://api.example.test", "ConsultarPedido", "k", "s", {"codigo_pedido": 3})
            self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
