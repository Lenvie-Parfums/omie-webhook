"""Consume only explicit portal requests; never re-run an uncertain inclusion."""
import logging
import threading
import time

log = logging.getLogger(__name__)


class PortalQueue:
    def __init__(self, bridge, process_order, interval=30):
        self.bridge = bridge
        self.process_order = process_order
        self.interval = max(10, interval)
        self.pending_result = None
        self.lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        self.last_result = None
        self.last_error = None

    def run_once(self):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if self.pending_result is None:
                response = self.bridge("claim", {})
                claim = response.get("claim") if isinstance(response, dict) else None
                if not claim:
                    return False
                result = {"id": claim["id"], "state": "error"}
                try:
                    body, _ = self.process_order(claim["order_id"])
                    result.update(
                        state="confirmed" if body.get("status") in {"transferido", "ja_existe"} else "error",
                        destination=body.get("destination"),
                        detail=body.get("detail") or "Sem confirmacao. Confira na 004 antes de reenviar.",
                    )
                except Exception:
                    result["detail"] = "Execucao inconclusiva. Confira se o pedido existe na 004 antes de reenviar."
                    log.exception("ATIVA_QUEUE_PROCESS_FAILED")
                self.pending_result = result
            # Retry only delivery of the result, never the business operation.
            response = self.bridge("result", self.pending_result)
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise RuntimeError("Portal did not acknowledge result")
            self.last_result = {"state": self.pending_result["state"], "at": time.time()}
            self.pending_result = None
            self.last_error = None
            return True
        except Exception:
            self.last_error = "Falha de comunicacao com o portal; nova tentativa de comunicacao sera automatica."
            log.warning("ATIVA_QUEUE_BRIDGE_FAILED")
            return False
        finally:
            self.lock.release()

    def _loop(self):
        while True:
            self.wake.clear()
            processed = self.run_once()
            # A full queue is drained sequentially. An empty queue calls no Omie API.
            self.wake.wait(2 if processed else self.interval)

    def start(self):
        with self.start_lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="ativa-portal-queue", daemon=True)
                self.thread.start()

    def notify(self):
        self.start()
        self.wake.set()

    def status(self):
        return {"running": bool(self.thread and self.thread.is_alive()),
                "pending_result": self.pending_result is not None,
                "last_result": self.last_result, "error": self.last_error}
