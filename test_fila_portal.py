import os
os.environ["ATIVA_QUEUE_ENABLED"] = "0"
import unittest
from unittest.mock import Mock, patch
from fila_portal import PortalQueue


class QueueTests(unittest.TestCase):
    def test_repeated_read_waits_30_seconds_but_distinct_reads_only_use_base_interval(self):
        import app
        clock = [100.0]
        delays = []
        def sleep(delay):
            delays.append(delay)
            clock[0] += delay
        app._omie_last_call = 0
        app._omie_read_calls.clear()
        with patch.object(app.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(app.time, 'sleep', side_effect=sleep), patch.object(app.requests, 'post', return_value=Mock(json=Mock(return_value={"ok": True}))) as post:
            for id in [1, 1, 2]:
                app.chamar_omie('https://api.example.test', 'ConsultarPedido', 'test-key', 'test-secret', {'id': id})
        self.assertEqual(post.call_count, 3)
        self.assertEqual(len(delays), 2)
        self.assertAlmostEqual(delays[0], 30)
        self.assertAlmostEqual(delays[1], 1.1)

    def test_empty_queue_does_not_consult_or_include_orders(self):
        process = Mock()
        bridge = Mock(return_value={"claim": None})
        self.assertFalse(PortalQueue(bridge, process).run_once())
        process.assert_not_called()

    def test_claim_process_and_confirm_destination(self):
        bridge = Mock(side_effect=[{"claim": {"id": "attempt", "order_id": "123"}}, {"ok": True}])
        process = Mock(return_value=({"status": "ja_existe", "destination": "115", "detail": "Ja existe"}, 200))
        queue = PortalQueue(bridge, process)
        self.assertTrue(queue.run_once())
        process.assert_called_once_with("123")
        self.assertEqual(bridge.call_args.args[1]["state"], "confirmed")
        self.assertEqual(bridge.call_args.args[1]["destination"], "115")

    def test_lost_callback_retries_only_callback_never_inclusion(self):
        bridge = Mock(side_effect=[{"claim": {"id": "attempt", "order_id": "123"}}, RuntimeError("network"), {"ok": True}])
        process = Mock(return_value=({"status": "transferido", "destination": "115"}, 200))
        queue = PortalQueue(bridge, process)
        self.assertFalse(queue.run_once())
        self.assertIsNotNone(queue.pending_result)
        self.assertTrue(queue.run_once())
        self.assertEqual(process.call_count, 1)
        self.assertEqual([c.args[0] for c in bridge.call_args_list], ["claim", "result", "result"])

    def test_uncertain_execution_is_error_not_success(self):
        bridge = Mock(side_effect=[{"claim": {"id": "attempt", "order_id": "123"}}, {"ok": True}])
        queue = PortalQueue(bridge, Mock(side_effect=RuntimeError("timeout")))
        self.assertTrue(queue.run_once())
        self.assertEqual(bridge.call_args.args[1]["state"], "error")

    def test_unacknowledged_callback_is_retained(self):
        bridge = Mock(side_effect=[{"claim": {"id": "attempt", "order_id": "123"}}, None])
        queue = PortalQueue(bridge, Mock(return_value=({"status": "erro", "detail": "Limite Omie"}, 200)))
        self.assertFalse(queue.run_once())
        self.assertEqual(queue.pending_result["state"], "error")

    def test_reentry_cannot_claim_another_order(self):
        bridge = Mock()
        queue = PortalQueue(bridge, Mock())
        queue.lock.acquire()
        self.assertFalse(queue.run_once())
        queue.lock.release()
        bridge.assert_not_called()

    def test_wake_route_requires_existing_bridge_secret(self):
        import app
        with patch.dict(os.environ, {"ATIVA_BRIDGE_TOKEN": "test-secret"}):
            client = app.app.test_client()
            self.assertEqual(client.post('/portal/ativa/processar-fila').status_code, 401)
            with patch.object(app, 'start_portal_queue', return_value=Mock(status=Mock(return_value={"running": True}))) as start:
                result = client.post('/portal/ativa/processar-fila', headers={"Authorization": "Bearer test-secret"})
                self.assertEqual(result.status_code, 202)
                start.return_value.notify.assert_called_once()

    def test_portal_processing_always_verifies_destination_before_inclusion(self):
        import app
        with patch.object(app, 'transferir_pedido_omie', return_value='ja_existe') as transfer:
            data, status = app.processar_pedido_portal('777')
            self.assertEqual(status, 200)
            self.assertEqual(data['status'], 'ja_existe')
            self.assertTrue(transfer.call_args.kwargs['reenvio'])


if __name__ == '__main__':
    unittest.main()
