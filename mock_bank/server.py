"""A sandbox banking partner for local development, tests, and demos.

Special destination accounts trigger specific behaviors, like the test
account numbers real bank sandboxes provide:

  EXT-CLOSED...   422 ACCOUNT_CLOSED        permanent failure, never retried
  EXT-INVALID...  400 INVALID_ACCOUNT       permanent failure, never retried
  EXT-DOWN...     503 every time            retried until the service gives up
  EXT-FLAKY...    503 twice, then success   succeeds after retries
  EXT-SLOW...     moves the money, but answers after the client has timed out
                  (first call only): the dangerous "did it go through?" case
  anything else   success, except random 503s at `failure_rate`

Like real bank APIs it honors Idempotency-Key: a repeated key returns the
original transfer and never moves money twice.
"""
import json
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockBank:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, failure_rate: float = 0.0,
                 flaky_failures: int = 2, ambiguous_delay_s: float = 8.0):
        self.failure_rate, self.flaky_failures, self.ambiguous_delay_s = failure_rate, flaky_failures, ambiguous_delay_s
        self.executed: list[dict] = []          # every transfer where money actually moved
        self.calls: dict[str, int] = {}         # requests received per idempotency key
        self._by_key: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer((host, port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://{host}:{self.port}"
        self._thread: threading.Thread | None = None

    def start(self) -> "MockBank":
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def transfers_for(self, reference: str) -> int:
        with self._lock:
            return sum(1 for t in self.executed if t["reference"] == reference)

    def _decide(self, key: str, body: dict) -> tuple[int, dict, float]:
        """Returns (status, response body, seconds to wait before answering)."""
        with self._lock:
            call = self.calls[key] = self.calls.get(key, 0) + 1
            if key in self._by_key:
                return 200, {"transferId": self._by_key[key]["transferId"], "status": "ACCEPTED", "replayed": True}, 0
            dest = str(body.get("destinationAccount", "")).upper()
            if dest.startswith("EXT-CLOSED"):
                return 422, {"code": "ACCOUNT_CLOSED", "message": "The destination account is closed."}, 0
            if dest.startswith("EXT-INVALID"):
                return 400, {"code": "INVALID_ACCOUNT", "message": "The destination account does not exist."}, 0
            if dest.startswith("EXT-DOWN") or (dest.startswith("EXT-FLAKY") and call <= self.flaky_failures):
                return 503, {"code": "BANK_UNAVAILABLE", "message": "Bank is temporarily unavailable."}, 0
            special = dest.startswith(("EXT-FLAKY", "EXT-SLOW"))
            if not special and random.random() < self.failure_rate:
                return 503, {"code": "RANDOM_OUTAGE", "message": "Simulated bank outage."}, 0
            transfer = {  # the money moves here
                "transferId": "trf_" + uuid.uuid4().hex[:20], "idempotencyKey": key,
                "sourceAccount": body["sourceAccount"], "destinationAccount": body["destinationAccount"],
                "amountCents": body["amountCents"], "reference": body["reference"],
                "executedAt": datetime.now(timezone.utc).isoformat(),
            }
            self._by_key[key] = transfer
            self.executed.append(transfer)
            delay = self.ambiguous_delay_s if dest.startswith("EXT-SLOW") and call == 1 else 0
            return 201, {"transferId": transfer["transferId"], "status": "ACCEPTED"}, delay

    def _handler(self):
        bank = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except ConnectionError:
                    pass  # the client already gave up (timeout scenario)

            def do_GET(self):
                if self.path == "/health":
                    return self._send(200, {"status": "ok"})
                if self.path == "/v1/ledger":
                    with bank._lock:
                        return self._send(200, {"executed": list(bank.executed), "calls": dict(bank.calls)})
                self._send(404, {"code": "NOT_FOUND"})

            def do_POST(self):
                if self.path != "/v1/transfers":
                    return self._send(404, {"code": "NOT_FOUND"})
                key = self.headers.get("Idempotency-Key")
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except ValueError:
                    body = {}
                required = ("sourceAccount", "destinationAccount", "amountCents", "reference")
                if not key or not all(body.get(f) for f in required) or not isinstance(body.get("amountCents"), int):
                    return self._send(400, {"code": "BAD_REQUEST", "message": "Missing or invalid transfer fields."})
                status, response, delay = bank._decide(key, body)
                if delay:
                    time.sleep(delay)
                self._send(status, response)

        return Handler
