"""Guided demo:  python scripts/demo.py

Runs the whole platform in-process against your PostgreSQL database, with the
sandbox bank and a webhook receiver, and walks through every scenario.
"""
import json
import sys
import threading
import warnings
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore", message=".*httpx.*")  # cosmetic library-version notice

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import create_customer_with_api_key  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import create_pool, migrate  # noqa: E402
from app.platform import Platform  # noqa: E402
from app.webhooks import verify_signature  # noqa: E402
from mock_bank.server import MockBank  # noqa: E402

RULE = "═" * 78

bank = MockBank(ambiguous_delay_s=2.5).start()
settings = Settings(bank_base_url=bank.url, bank_timeout_ms=1000, breaker_failure_threshold=1000, job_lease_ms=5000, max_attempts=5, retry_base_ms=400,
                    retry_max_ms=2000, poll_interval_ms=100, webhook_block_private_ips=False, webhook_retry_base_ms=300,
                    log_level="ERROR")
pool = create_pool(settings.database_url, 1)
migrate(pool)
pool.close()

platform = Platform(settings)
customer_id = f"DEMO-{int(time.time())}"
api_key = create_customer_with_api_key(platform.pool, customer_id, "Demo Customer")["api_key"]
client = TestClient(platform.api, headers={"Authorization": f"Bearer {api_key}"})

# The customer's webhook server: verifies every signature it receives.
secret = {"value": ""}
webhooks: list[dict] = []


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
        webhooks.append({"type": json.loads(raw)["type"], "valid": verify_signature(secret["value"], raw, self.headers["ACH-Signature"])})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
threading.Thread(target=receiver.serve_forever, daemon=True).start()

print(RULE)
print(" ACH PAYMENT PLATFORM: LIVE DEMO")
print(RULE)
print(f"Customer {customer_id} authenticated with an API key (stored only as a SHA-256 hash).")
endpoint = client.post("/v1/webhook-endpoints", json={"url": f"http://127.0.0.1:{receiver.server_address[1]}/hooks"}).json()
secret["value"] = endpoint["secret"]
print(f"Webhook endpoint registered: {endpoint['id']}\n")

platform.worker.start()
platform.dispatcher.start()

scenarios = [
    ("Normal payment", "EXT98765"),
    ("Bank has a temporary outage (fails twice, then works)", "EXT-FLAKY-1"),
    ("Destination account is closed (permanent error)", "EXT-CLOSED-1"),
    ("Bank is down for good", "EXT-DOWN-1"),
    ("Bank moves the money but replies too late (timeout)", "EXT-SLOW-1"),
]
submitted = []
for i, (title, account) in enumerate(scenarios, 1):
    reference = f"{customer_id}-PMT-{i}"
    res = client.post("/v1/payments", headers={"Idempotency-Key": f"{reference}-key"},
                      json={"customerId": customer_id, "sourceAccount": "VA10001", "destinationAccount": account,
                            "amount": 250.00, "reference": reference})
    print(f"→ {title:<56} HTTP {res.status_code}  {res.json()['status']}")
    submitted.append((title, res.json()["id"], reference))

print("\nDuplicate protection:")
first_ref = submitted[0][2]
duplicate = {"customerId": customer_id, "sourceAccount": "VA10001", "destinationAccount": "EXT98765", "amount": "250.00", "reference": first_ref}
replay = client.post("/v1/payments", headers={"Idempotency-Key": f"{first_ref}-key"}, json=duplicate)
print(f"→ Client retries the same request with the same key:  HTTP {replay.status_code}, same payment returned")
new_key = client.post("/v1/payments", headers={"Idempotency-Key": "a-brand-new-key-123"}, json=duplicate)
print(f"→ Same payment reference under a new key:            HTTP {new_key.status_code} {new_key.json()['error']['code']}")

print("\nProcessing in the background (the API already answered; workers do the bank calls)...")
while True:
    with platform.pool.connection() as conn:
        row = conn.execute(
            """SELECT (SELECT count(*) FROM payments WHERE customer_id = %s AND status NOT IN ('COMPLETED','FAILED')) AS open,
                      (SELECT count(*) FROM webhook_deliveries d JOIN webhook_endpoints e ON e.id = d.endpoint_id
                       WHERE e.customer_id = %s AND d.state IN ('PENDING','DELIVERING')) AS pending""",
            (customer_id, customer_id)).fetchone()
    if row["open"] == 0 and row["pending"] == 0:
        break
    time.sleep(0.2)

for title, pid, _ in submitted:
    payment = client.get(f"/v1/payments/{pid}").json()
    error = f"   ({payment['lastError']['code']})" if payment["lastError"] else ""
    print(f"\n{'─' * 78}\n{title}\nFinal: {payment['status']} after {payment['attempts']} attempt(s){error}")
    for e in client.get(f"/v1/payments/{pid}/events").json()["events"]:
        print(f"  {(e['from'] or '∅'):<10} → {e['to']:<10}  {e['reason']}")

slow_title, slow_id, slow_ref = submitted[4]
print(f"\n{RULE}")
print(f"Bank ledger: {len(bank.executed)} transfers executed (normal, flaky, slow). Closed/down moved no money.")
print(f"Timeout case: the bank was called {bank.calls[slow_id]} times but moved the money {bank.transfers_for(slow_ref)} time(s).")
print(f"Webhooks received: {len(webhooks)}, valid signatures: {sum(w['valid'] for w in webhooks)}")
print(f"Webhook types seen: {', '.join(dict.fromkeys(w['type'] for w in webhooks))}")
print(RULE)

receiver.shutdown()
platform.close()
bank.stop()
