"""Chaos eval: everything at once, then check the guarantees that must ALWAYS hold.
  - 3 competing worker processes + 2 competing webhook dispatchers
  - the bank randomly fails 30% of calls; the customer's webhook server randomly fails 25%
  - 80 payments, each submitted 3 times concurrently (2 exact duplicates + 1 new key, same reference)
"""
import random
from concurrent.futures import ThreadPoolExecutor

from app.webhooks import verify_signature
from tests.harness import assert_valid_chain, new_payment


def test_never_double_pays_never_loses_a_payment_never_loses_or_reorders_an_event(harnesses, receivers):
    h = harnesses(bank={"failure_rate": 0.3}, max_attempts=10, webhook_max_attempts=15, worker_concurrency=3)
    r = receivers(lambda n: 500 if random.random() < 0.25 else 200)
    endpoint = h.request("POST", "/v1/webhook-endpoints", json_body={"url": r.url}).json()

    h.start()
    h.extra_worker("chaos-worker-2").start()
    h.extra_worker("chaos-worker-3").start()
    h.extra_dispatcher().start()

    payments = [new_payment() for _ in range(80)]
    jobs = [(p, f"chaos-{p['reference']}") for p in payments] * 2 + [(p, f"other-{p['reference']}") for p in payments]
    random.shuffle(jobs)
    with ThreadPoolExecutor(max_workers=24) as pool:
        responses = list(pool.map(lambda job: h.submit(*job), jobs))

    # 1. The API never errors: every response is an expected outcome.
    assert {res.status_code for res in responses} <= {200, 202, 409}

    h.wait_for_quiet(timeout=90)
    rows = h.query("SELECT id, reference, status FROM payments")
    assert len(rows) == 80  # 2. exactly one payment per reference

    completed = 0
    for p in rows:
        assert p["status"] in ("COMPLETED", "FAILED")  # 3. every payment reached a final state
        # 4. money moved exactly once if COMPLETED, never if FAILED
        assert h.bank.transfers_for(p["reference"]) == (1 if p["status"] == "COMPLETED" else 0), p["reference"]
        completed += p["status"] == "COMPLETED"
        assert_valid_chain(h.events(p["id"]), p["status"])  # 5. unbroken audit chain
    assert len(h.bank.executed) == completed

    # 6. every audit event reached the customer (deduplicated by event id), correctly signed
    total_events = h.query("SELECT count(*) AS n FROM payment_events")[0]["n"]
    assert len({w["json"]["id"] for w in r.received}) == total_events
    assert all(verify_signature(endpoint["secret"], w["raw"], w["headers"]["ACH-Signature"]) for w in r.received)

    # 7. per payment, webhooks arrived in audit-trail order (2 dispatchers + retries notwithstanding)
    first_seen: dict[str, list[int]] = {}
    seen = set()
    for w in r.received:
        if w["json"]["id"] in seen:
            continue
        seen.add(w["json"]["id"])
        first_seen.setdefault(w["json"]["data"]["payment"]["id"], []).append(w["json"]["sequence"])
    for payment_id, sequences in first_seen.items():
        assert sequences == sorted(sequences), payment_id
