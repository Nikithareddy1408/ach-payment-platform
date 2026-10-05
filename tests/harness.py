"""Starts the REAL system for a test: PostgreSQL, the HTTP API (uvicorn), the
workers, the webhook dispatcher, and the sandbox bank, with fast timings.
Nothing is mocked except the bank, which is a real HTTP server."""
import itertools
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import uvicorn

from app.auth import create_customer_with_api_key
from app.bank import CircuitBreaker, HttpBankGateway
from app.config import Settings
from app.platform import Platform
from app.webhooks import WebhookDispatcher
from app.worker import PaymentWorker
from mock_bank.server import MockBank
from tests.conftest import TEST_DATABASE_URL

TEST_SETTINGS = dict(
    environment="test", database_url=TEST_DATABASE_URL, database_pool_size=40, log_level="WARNING",
    bank_timeout_ms=150, job_lease_ms=2000, max_attempts=4, retry_base_ms=5, retry_max_ms=20,
    poll_interval_ms=5, worker_concurrency=2, breaker_failure_threshold=100_000, breaker_cooldown_ms=100,
    webhook_timeout_ms=500, webhook_max_attempts=4, webhook_retry_base_ms=5, webhook_retry_max_ms=20,
    webhook_concurrency=2, webhook_block_private_ips=False, rate_limit_per_minute=100_000,
)
_counter = itertools.count(1)


def new_payment(**overrides) -> dict:
    n = next(_counter)
    return {"customerId": "C12345", "sourceAccount": "VA10001", "destinationAccount": "EXT98765",
            "amount": 250.00, "reference": f"PMT-{int(time.time() * 1000)}-{n}", **overrides}


def wait_for(check, timeout: float = 10.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out after {timeout}s waiting for {what}")
        time.sleep(0.01)


def assert_valid_chain(events: list[dict], final_status: str) -> None:
    """Audit invariant: a gap-free chain None -> PENDING -> ... -> current status."""
    assert events, "payment has no audit events"
    assert events[0]["from"] is None and events[0]["to"] == "PENDING"
    for previous, current in zip(events, events[1:]):
        assert current["from"] == previous["to"], f"gap in audit chain: {previous} -> {current}"
    assert events[-1]["to"] == final_status


class Harness:
    def __init__(self, bank: dict | None = None, **settings_overrides):
        self.bank = MockBank(**{"failure_rate": 0.0, "ambiguous_delay_s": 0.4, **(bank or {})}).start()
        self.settings = Settings(_env_file=None, **{**TEST_SETTINGS, "bank_base_url": self.bank.url, **settings_overrides})
        self.platform = Platform(self.settings)
        with self.platform.pool.connection() as conn:
            conn.execute("TRUNCATE webhook_deliveries, webhook_endpoints, payment_jobs, payment_events, payments, api_keys, customers "
                         "RESTART IDENTITY CASCADE")
        self.api_key = create_customer_with_api_key(self.platform.pool, "C12345", "Acme Corp")["api_key"]
        self._extras: list = []

        config = uvicorn.Config(self.platform.api, host="127.0.0.1", port=0, log_config=None, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        wait_for(lambda: self._server.started, 10, "API server to start")
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30)
        self._closed = False

    # ── HTTP helpers ──
    def request(self, method: str, path: str, *, json_body=None, content=None, headers=None, api_key="default") -> httpx.Response:
        key = self.api_key if api_key == "default" else api_key
        all_headers = {**({"Authorization": f"Bearer {key}"} if key else {}), **(headers or {})}
        return self.http.request(method, path, json=json_body, content=content, headers=all_headers)

    def submit(self, payment: dict, key: str | None = None, api_key="default") -> httpx.Response:
        return self.request("POST", "/v1/payments", json_body=payment,
                            headers={"Idempotency-Key": key or f"idem-{payment['reference']}"}, api_key=api_key)

    def submit_concurrently(self, payment: dict, key: str, copies: int) -> list[httpx.Response]:
        with ThreadPoolExecutor(max_workers=copies) as pool:
            return list(pool.map(lambda _: self.submit(payment, key), range(copies)))

    def events(self, payment_id: str) -> list[dict]:
        return self.request("GET", f"/v1/payments/{payment_id}/events").json()["events"]

    # ── Background processing ──
    def start(self) -> None:
        self.platform.worker.start()
        self.platform.dispatcher.start()

    def extra_worker(self, instance_id: str) -> PaymentWorker:
        s = self.settings
        worker = PaymentWorker(self.platform.pool, s, self.platform.payments, HttpBankGateway(s.bank_base_url, s.bank_timeout_ms),
                               CircuitBreaker(s.breaker_failure_threshold, s.breaker_cooldown_ms), self.platform.metrics, instance_id)
        self._extras.append(worker)
        return worker

    def extra_dispatcher(self) -> WebhookDispatcher:
        dispatcher = WebhookDispatcher(self.platform.pool, self.settings, self.platform.metrics)
        self._extras.append(dispatcher)
        return dispatcher

    def wait_for_final(self, payment_id: str, timeout: float = 10.0) -> dict:
        def done():
            body = self.request("GET", f"/v1/payments/{payment_id}").json()
            return body if body["status"] in ("COMPLETED", "FAILED") else None
        return wait_for(done, timeout, f"payment {payment_id} to finish")

    def wait_for_quiet(self, timeout: float = 20.0) -> None:
        def quiet():
            with self.platform.pool.connection() as conn:
                row = conn.execute(
                    "SELECT (SELECT count(*) FROM payments WHERE status NOT IN ('COMPLETED','FAILED')) AS open, "
                    "(SELECT count(*) FROM webhook_deliveries WHERE state IN ('PENDING','DELIVERING')) AS pending"
                ).fetchone()
            return row["open"] == 0 and row["pending"] == 0
        wait_for(quiet, timeout, "all payments and webhooks to settle")

    def query(self, sql: str, params=()) -> list[dict]:
        with self.platform.pool.connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall() if cursor.description else []  # UPDATE/INSERT return no rows

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for extra in self._extras:
            extra.stop()
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self.http.close()
        self.platform.close()
        self.bank.stop()


class Receiver:
    """A customer's webhook server. respond(n) picks the HTTP status for the nth call."""

    def __init__(self, respond=lambda n: 200):
        self.received: list[dict] = []
        self.calls = 0
        lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                with lock:
                    receiver.calls += 1
                    status = respond(receiver.calls)
                    if 200 <= status < 300:
                        receiver.received.append({"headers": dict(self.headers), "raw": raw, "json": json.loads(raw)})
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hooks"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
