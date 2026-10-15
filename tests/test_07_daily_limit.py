"""Requirement R14: daily payment limit per customer. A customer may submit at most
DAILY_LIMIT_CENTS (default $50,000.00) per UTC calendar day, counting all of that day's
payments that are not FAILED. Over the limit: 422 DAILY_LIMIT_EXCEEDED and nothing is stored.
Must hold under concurrent requests."""
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.auth import create_customer_with_api_key
from app.config import Settings
from app.db import create_pool
from app.payments import Actor, NewPayment, PaymentService
from tests.conftest import TEST_DATABASE_URL
from tests.harness import new_payment

LIMIT = 5_000_000
START_OF_TODAY_UTC = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"


@pytest.fixture
def h(harnesses):
    return harnesses(daily_limit_cents=LIMIT)


def counts(h) -> dict:
    return h.query("SELECT (SELECT count(*) FROM payments) AS payments, (SELECT count(*) FROM payment_events) AS events, "
                   "(SELECT count(*) FROM payment_jobs) AS jobs, (SELECT count(*) FROM webhook_deliveries) AS webhooks")[0]


def assert_limit_exceeded(res, used: int, requested: int) -> None:
    assert res.status_code == 422, res.text
    error = res.json()["error"]
    assert error["code"] == "DAILY_LIMIT_EXCEEDED"
    assert isinstance(error["message"], str) and error["message"]
    assert error["details"] == {"limitCents": LIMIT, "usedCents": used, "requestedCents": requested}


def insert_payment(h, amount_cents: int, created_at_sql: str, status: str = "PENDING", customer_id: str = "C12345") -> None:
    ref = new_payment()["reference"]
    h.query(f"INSERT INTO payments (id, customer_id, source_account, destination_account, amount_cents, reference, status, "
            f"idempotency_key, request_hash, created_at) VALUES (%s, %s, 'VA10001', 'EXT98765', %s, %s, %s, %s, 'x', {created_at_sql})",
            ("pay_" + ref.replace("-", "").lower().ljust(32, "0")[-32:], customer_id, amount_cents, ref, status, "idem-" + ref))


class TestSetting:
    def test_default_is_50000_dollars(self, monkeypatch):
        monkeypatch.delenv("DAILY_LIMIT_CENTS", raising=False)
        assert Settings(_env_file=None).daily_limit_cents == 5_000_000

    def test_configurable_with_daily_limit_cents(self, monkeypatch):
        monkeypatch.setenv("DAILY_LIMIT_CENTS", "123456")
        assert Settings(_env_file=None).daily_limit_cents == 123456

    def test_limit_is_applied_from_the_setting(self, harnesses):
        small = harnesses(daily_limit_cents=1000)
        assert small.submit(new_payment(amount="10.00")).status_code == 202
        res = small.submit(new_payment(amount="0.01"))
        assert res.status_code == 422 and res.json()["error"]["details"] == {"limitCents": 1000, "usedCents": 1000, "requestedCents": 1}


class TestWithinLimit:
    def test_payments_up_to_exactly_the_limit_are_accepted_unchanged(self, h):
        first = h.submit(new_payment(amount="49999.99"))
        assert first.status_code == 202 and first.json()["status"] == "PENDING"
        last = h.submit(new_payment(amount="0.01"))
        assert last.status_code == 202, last.text
        assert last.json()["amountCents"] == 1 and last.headers["idempotent-replayed"] == "false"

    def test_failed_payments_do_not_count(self, h):
        h.start()
        failed = h.submit(new_payment(amount="50000.00", destinationAccount="EXT-CLOSED-01")).json()["id"]
        assert h.wait_for_final(failed)["status"] == "FAILED"
        assert h.submit(new_payment(amount="50000.00")).status_code == 202

    def test_payments_from_yesterday_utc_do_not_count(self, h):
        insert_payment(h, LIMIT, f"{START_OF_TODAY_UTC} - interval '1 microsecond'")
        assert h.submit(new_payment(amount="50000.00")).status_code == 202

    def test_other_customers_payments_do_not_count(self, h):
        other_key = create_customer_with_api_key(h.platform.pool, "C99999", "Other Corp")["api_key"]
        assert h.submit(new_payment(customerId="C99999", amount="50000.00"), api_key=other_key).status_code == 202
        assert h.submit(new_payment(amount="50000.00")).status_code == 202


class TestOverLimit:
    def test_over_the_limit_gives_422_with_details_and_nothing_is_stored(self, h):
        assert h.submit(new_payment(amount="40000.00")).status_code == 202
        before = counts(h)
        body = new_payment(amount="10000.01")
        res = h.submit(body)
        assert_limit_exceeded(res, used=4_000_000, requested=1_000_001)
        assert counts(h) == before
        assert h.query("SELECT 1 FROM payments WHERE reference = %s", (body["reference"],)) == []

    def test_a_single_payment_over_the_limit_is_rejected(self, h):
        assert_limit_exceeded(h.submit(new_payment(amount="50000.01")), used=0, requested=5_000_001)
        assert counts(h)["payments"] == 0

    def test_every_status_except_failed_counts(self, h):
        for status in ("PENDING", "PROCESSING", "RETRYING", "COMPLETED"):
            insert_payment(h, 1_000_000, "now()", status)
        insert_payment(h, 1_000_000, "now()", "FAILED")
        assert_limit_exceeded(h.submit(new_payment(amount="10000.01")), used=4_000_000, requested=1_000_001)

    def test_returned_payments_count(self, h):
        h.start()
        pid = h.submit(new_payment(amount="50000.00")).json()["id"]
        assert h.wait_for_final(pid)["status"] == "COMPLETED"
        assert h.request("POST", f"/v1/payments/{pid}/returns", json_body={"returnCode": "R01"}).status_code == 200
        assert_limit_exceeded(h.submit(new_payment(amount="0.01")), used=LIMIT, requested=1)

    def test_payment_at_the_start_of_today_utc_counts(self, h):
        insert_payment(h, LIMIT, START_OF_TODAY_UTC)
        assert_limit_exceeded(h.submit(new_payment(amount="0.01")), used=LIMIT, requested=1)


class TestIdempotentRetryAtLimit:
    def test_retry_returns_200_with_the_original_even_at_the_limit(self, h):
        body = new_payment(amount="50000.00")
        original = h.submit(body, "at-limit-key-0001")
        assert original.status_code == 202
        retry = h.submit(body, "at-limit-key-0001")
        assert retry.status_code == 200, retry.text
        assert retry.json() == original.json() and retry.headers["idempotent-replayed"] == "true"
        assert counts(h)["payments"] == 1

    def test_retry_of_an_earlier_payment_after_reaching_the_limit(self, h):
        early = new_payment(amount="30000.00")
        first = h.submit(early, "early-key-0001")
        assert h.submit(new_payment(amount="20000.00")).status_code == 202
        assert_limit_exceeded(h.submit(new_payment(amount="0.01")), used=LIMIT, requested=1)
        retry = h.submit(early, "early-key-0001")
        assert retry.status_code == 200 and retry.json()["id"] == first.json()["id"]


class TestConcurrency:
    def test_burst_of_simultaneous_payments_never_exceeds_the_limit(self, h):
        bodies = [new_payment(amount="10000.00") for _ in range(12)]
        with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
            results = list(pool.map(h.submit, bodies))
        codes = sorted(r.status_code for r in results)
        assert codes == [202] * 5 + [422] * 7, codes
        for r in results:
            if r.status_code == 422:
                assert r.json()["error"]["code"] == "DAILY_LIMIT_EXCEEDED"
        assert h.query("SELECT sum(amount_cents)::bigint AS total FROM payments")[0]["total"] == LIMIT

    def test_second_request_during_first_requests_limit_check_cannot_slip_under(self, h):
        """Forces the race EVERY time: request A has read the day's total but not yet inserted
        when request B arrives. B must wait for A and then see A's payment."""
        a_checked, b_done = threading.Event(), threading.Event()
        pool_a = create_pool(TEST_DATABASE_URL, 2)
        original_connection = pool_a.connection

        @contextmanager
        def intercepted_connection(*args, **kwargs):
            with original_connection(*args, **kwargs) as conn:
                real_execute = conn.execute

                def execute(query, *a, **kw):
                    result = real_execute(query, *a, **kw)
                    if "sum(amount_cents)" in str(query) and not a_checked.is_set():
                        a_checked.set()
                        b_done.wait(timeout=1.5)  # without serialization, B commits here
                    return result
                conn.execute = execute
                try:
                    yield conn
                finally:
                    del conn.execute

        pool_a.connection = intercepted_connection
        outcome = {}

        def request_a():
            body = new_payment(amount="30000.00")
            service = PaymentService(pool_a, h.settings, h.platform.metrics)
            try:
                outcome["a"] = service.create(body["customerId"], NewPayment(
                    body["customerId"], body["sourceAccount"], body["destinationAccount"], body["amount"], body["reference"]),
                    "race-key-a-0001", Actor("api"))
            except Exception as error:  # noqa: BLE001
                outcome["a"] = error

        thread_a = threading.Thread(target=request_a)
        try:
            thread_a.start()
            assert a_checked.wait(timeout=10), "request A never checked the daily total"
            res_b = h.submit(new_payment(amount="30000.00"), "race-key-b-0001")
            b_done.set()
            thread_a.join(timeout=10)
        finally:
            b_done.set()
            thread_a.join(timeout=10)
            pool_a.close()

        payment_a, replayed = outcome["a"]
        assert replayed is False and payment_a["amount_cents"] == 3_000_000
        assert_limit_exceeded(res_b, used=3_000_000, requested=3_000_000)
        assert h.query("SELECT sum(amount_cents)::bigint AS total FROM payments")[0]["total"] == 3_000_000
