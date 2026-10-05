"""PDF requirements: "Duplicate Requests" and "Asynchronous Processing"."""
import hashlib
import json

import psycopg
import pytest

from app.auth import create_customer_with_api_key
from app.db import create_pool
from app.payments import Actor, NewPayment, PaymentService
from tests.conftest import TEST_DATABASE_URL
from tests.harness import new_payment


class TestDuplicateRequests:
    def test_same_key_same_body_returns_the_original(self, h):
        body = new_payment()
        first, second = h.submit(body, "dup-key-0001"), h.submit(body, "dup-key-0001")
        assert first.status_code == 202 and second.status_code == 200
        assert second.headers["idempotent-replayed"] == "true"
        assert second.json()["id"] == first.json()["id"]
        assert h.query("SELECT (SELECT count(*) FROM payments) AS p, (SELECT count(*) FROM payment_jobs) AS j")[0] == {"p": 1, "j": 1}

    def test_equivalent_amounts_are_the_same_request(self, h):
        base = new_payment()
        ids = {h.submit({**base, "amount": amount}, "same-amount-key").json()["id"] for amount in (250, 250.0, "250", "250.00")}
        assert len(ids) == 1

    def test_same_key_different_body_is_422(self, h):
        body = new_payment()
        h.submit(body, "reused-key-0001")
        res = h.submit({**body, "amount": 999.99}, "reused-key-0001")
        assert res.status_code == 422 and res.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_new_key_same_reference_is_409_pointing_to_original(self, h):
        body = new_payment()
        first = h.submit(body, "first-key-0001").json()
        res = h.submit(body, "second-key-0002")
        assert res.status_code == 409
        assert res.json()["error"]["details"]["existingPaymentId"] == first["id"]

    def test_30_identical_requests_at_once_create_one_payment_and_one_transfer(self, h):
        h.start()
        body = new_payment()
        results = h.submit_concurrently(body, "burst-key-0001", 30)
        assert sorted(r.status_code for r in results) == [200] * 29 + [202]
        assert len({r.json()["id"] for r in results}) == 1
        h.wait_for_final(results[0].json()["id"])
        assert h.bank.transfers_for(body["reference"]) == 1

    def test_regression_duplicate_committing_mid_check_gets_200_not_a_false_409(self, h):
        """Found by this suite: two separate SELECTs could straddle a concurrent commit (key check misses,
        reference check hits -> wrong 409). This test forces that exact interleaving EVERY time."""
        body = new_payment()
        key = "interleave-key-0001"
        request_hash = hashlib.sha256(json.dumps(
            [body["customerId"], body["sourceAccount"], body["destinationAccount"], 25000, body["reference"]]).encode()).hexdigest()

        # Request A: inserted but NOT yet committed.
        conn_a = psycopg.connect(TEST_DATABASE_URL)
        conn_a.execute(
            "INSERT INTO payments (id, customer_id, source_account, destination_account, amount_cents, reference, status, "
            "idempotency_key, request_hash) VALUES (%s, %s, %s, %s, 25000, %s, 'PENDING', %s, %s)",
            ("pay_" + "a" * 32, body["customerId"], body["sourceAccount"], body["destinationAccount"], body["reference"], key, request_hash),
        )

        # Request B (a retry with the same key): A commits right after B's FIRST lookup query.
        pool_b = create_pool(TEST_DATABASE_URL, 2)
        fired = []
        original_connection = pool_b.connection

        from contextlib import contextmanager

        @contextmanager
        def intercepted_connection(*args, **kwargs):
            with original_connection(*args, **kwargs) as conn:
                real_execute = conn.execute

                def execute(query, *a, **kw):
                    result = real_execute(query, *a, **kw)
                    if not fired and "FROM payments WHERE customer_id" in str(query):
                        fired.append(True)
                        conn_a.commit()
                    return result
                conn.execute = execute
                try:
                    yield conn
                finally:
                    del conn.execute

        pool_b.connection = intercepted_connection
        try:
            service = PaymentService(pool_b, h.settings, h.platform.metrics)
            payment, replayed = service.create(body["customerId"], NewPayment(
                body["customerId"], body["sourceAccount"], body["destinationAccount"], body["amount"], body["reference"]),
                key, Actor("api"))
            assert fired, "the interleaving was not triggered"
            assert replayed is True
            assert payment["id"] == "pay_" + "a" * 32
        finally:
            pool_b.close()
            conn_a.close()

    def test_same_reference_allowed_for_different_customers(self, h):
        other = create_customer_with_api_key(h.platform.pool, "C2", "Second")["api_key"]
        a = h.submit(new_payment(reference="INV-77"), "c1-key-0001")
        b = h.submit(new_payment(customerId="C2", reference="INV-77"), "c2-key-0001", api_key=other)
        assert a.status_code == b.status_code == 202 and a.json()["id"] != b.json()["id"]

    def test_database_rejects_duplicate_reference_even_if_code_is_bypassed(self, h):
        h.submit(new_payment(reference="RAW-1"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            h.query("INSERT INTO payments (id, customer_id, source_account, destination_account, amount_cents, reference, status, "
                    "idempotency_key, request_hash) VALUES ('pay_raw', 'C12345', 'A1111', 'B2222', 100, 'RAW-1', 'PENDING', 'k', 'x')")


class TestAsynchronousProcessing:
    def test_responds_before_the_bank_is_called(self, h):
        res = h.submit(new_payment())  # workers NOT started
        assert res.status_code == 202 and res.json()["status"] == "PENDING"
        assert h.bank.calls == {}
        assert h.query("SELECT state FROM payment_jobs WHERE payment_id = %s", (res.json()["id"],))[0]["state"] == "QUEUED"
        h.start()
        assert h.wait_for_final(res.json()["id"])["status"] == "COMPLETED"
        assert len(h.bank.executed) == 1

    def test_queued_payments_survive_a_restart(self, h):
        res = h.submit(new_payment())
        h.extra_worker("brand-new-process").start()  # a different process, same database
        assert h.wait_for_final(res.json()["id"])["status"] == "COMPLETED"

    def test_competing_workers_process_each_payment_exactly_once(self, h):
        responses = [h.submit(new_payment()) for _ in range(60)]
        for i in range(4):
            h.extra_worker(f"competing-{i}").start()
        for r in responses:
            h.wait_for_final(r.json()["id"], timeout=30)
        assert len(h.bank.executed) == 60
        assert len({t["reference"] for t in h.bank.executed}) == 60
        assert all(h.bank.calls[r.json()["id"]] == 1 for r in responses)
