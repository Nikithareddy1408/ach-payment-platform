"""PDF requirements: "Payment Lifecycle", "Retry handling", "External bank API integrations",
and the business context: banks are not always available, networks fail, the platform must recover safely."""
import time

import httpx
import psycopg
import pytest

from app.bank import CircuitBreaker
from app.db import transaction
from app.domain import STATUSES, backoff_ms, can_transition
from app.job_queue import claim_job, holds_lease
from app.payments import Actor
from tests.harness import assert_valid_chain, new_payment


def statuses(h, payment_id):
    return [e["to"] for e in h.events(payment_id)]


class TestLifecycle:
    def test_exactly_5_legal_transitions_out_of_25_pairs(self):
        legal = {("PENDING", "PROCESSING"), ("PROCESSING", "COMPLETED"), ("PROCESSING", "FAILED"),
                 ("PROCESSING", "RETRYING"), ("RETRYING", "PROCESSING")}
        for a in STATUSES:
            for b in STATUSES:
                assert can_transition(a, b) == ((a, b) in legal), f"{a} -> {b}"

    def test_database_refuses_illegal_transitions_even_from_raw_sql(self, h):
        pid = h.submit(new_payment()).json()["id"]
        with pytest.raises(psycopg.errors.CheckViolation, match="illegal payment status transition PENDING -> COMPLETED"):
            h.query("UPDATE payments SET status = 'COMPLETED' WHERE id = %s", (pid,))

    def test_database_refuses_changes_to_payment_terms(self, h):
        pid = h.submit(new_payment()).json()["id"]
        for sql in ("UPDATE payments SET amount_cents = 1 WHERE id = %s", "UPDATE payments SET destination_account = 'EVIL1' WHERE id = %s"):
            with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
                h.query(sql, (pid,))

    def test_happy_path(self, h):
        h.start()
        pid = h.submit(new_payment()).json()["id"]
        final = h.wait_for_final(pid)
        assert final["status"] == "COMPLETED" and final["attempts"] == 1 and final["lastError"] is None
        assert final["bankTransferId"].startswith("trf_") and final["completedAt"]
        assert statuses(h, pid) == ["PENDING", "PROCESSING", "COMPLETED"]


class TestRetries:
    def test_temporary_outage_retries_then_completes(self, h):
        h.start()
        pid = h.submit(new_payment(destinationAccount="EXT-FLAKY-01")).json()["id"]
        final = h.wait_for_final(pid)
        assert final["status"] == "COMPLETED" and final["attempts"] == 3
        assert statuses(h, pid) == ["PENDING", "PROCESSING", "RETRYING", "PROCESSING", "RETRYING", "PROCESSING", "COMPLETED"]

    @pytest.mark.parametrize("account, code", [("EXT-CLOSED-01", "ACCOUNT_CLOSED"), ("EXT-INVALID-01", "INVALID_ACCOUNT")])
    def test_permanent_rejection_fails_immediately_without_retrying(self, h, account, code):
        h.start()
        pid = h.submit(new_payment(destinationAccount=account)).json()["id"]
        final = h.wait_for_final(pid)
        assert final["status"] == "FAILED" and final["attempts"] == 1 and final["lastError"]["code"] == code
        assert h.bank.calls[pid] == 1 and h.bank.executed == []

    def test_bank_down_for_good_fails_after_max_attempts(self, h):
        h.start()
        pid = h.submit(new_payment(destinationAccount="EXT-DOWN-01")).json()["id"]
        final = h.wait_for_final(pid)
        assert final["status"] == "FAILED" and final["attempts"] == 4 and final["lastError"]["code"] == "BANK_UNAVAILABLE"
        assert h.bank.calls[pid] == 4
        assert "Gave up after 4 attempts" in h.events(pid)[-1]["reason"]

    def test_backoff_grows_exponentially_with_jitter_and_a_cap(self):
        for attempt in range(1, 13):
            ceiling = min(60_000, 1000 * 2 ** (attempt - 1))
            assert backoff_ms(attempt, 1000, 60_000, rand=0) == round(ceiling / 2)
            assert backoff_ms(attempt, 1000, 60_000, rand=1) == ceiling


class TestMoneyNeverMovesTwice:
    def test_bank_timeout_after_money_moved_does_not_pay_twice(self, h):
        h.start()  # client timeout 0.15s, the bank answers after 0.4s
        p = new_payment(destinationAccount="EXT-SLOW-01")
        pid = h.submit(p).json()["id"]
        assert h.wait_for_final(pid)["status"] == "COMPLETED"
        assert h.bank.calls[pid] >= 2
        assert h.bank.transfers_for(p["reference"]) == 1
        events = h.events(pid)
        assert any(e["metadata"].get("code") == "BANK_TIMEOUT" for e in events)
        assert events[-1]["metadata"]["idempotentReplayByBank"] is True

    def test_worker_crash_after_bank_moved_money_is_recovered_without_paying_twice(self, h):
        p = new_payment()
        pid = h.submit(p).json()["id"]
        # Worker A: claims the job, marks PROCESSING, the bank executes... then A dies.
        claim_job(h.platform.pool, "worker-A", 60_000)
        with transaction(h.platform.pool) as conn:
            h.platform.payments.transition(conn, pid, "PROCESSING", reason="Attempt 1 (worker will crash)", actor=Actor("worker:A"), attempts=1)
        httpx.post(f"{h.bank.url}/v1/transfers", headers={"Idempotency-Key": pid},
                   json={"sourceAccount": p["sourceAccount"], "destinationAccount": p["destinationAccount"],
                         "amountCents": 25000, "reference": p["reference"]})
        assert len(h.bank.executed) == 1
        # Time passes: A's lease expires. Worker B starts.
        h.query("UPDATE payment_jobs SET lease_expires_at = now() - interval '1 second' WHERE payment_id = %s", (pid,))
        h.start()
        assert h.wait_for_final(pid)["status"] == "COMPLETED"
        assert len(h.bank.executed) == 1
        events = h.events(pid)
        assert any(e["metadata"].get("recoveredFromInterruptedAttempt") for e in events)
        assert_valid_chain(events, "COMPLETED")

    def test_zombie_worker_with_expired_lease_cannot_record_a_result(self, h):
        h.submit(new_payment())
        zombie = claim_job(h.platform.pool, "zombie", 60_000)
        h.query("UPDATE payment_jobs SET lease_expires_at = now() - interval '1 second'")
        takeover = claim_job(h.platform.pool, "new-owner", 60_000)
        assert takeover.payment_id == zombie.payment_id and takeover.recovered
        with transaction(h.platform.pool) as conn:
            assert holds_lease(conn, zombie) is False
            assert holds_lease(conn, takeover) is True


class TestCircuitBreaker:
    def test_opens_after_failures_allows_one_trial_then_closes(self):
        now = [0.0]
        changes = []
        breaker = CircuitBreaker(3, 1000, changes.append, clock=lambda: now[0])
        for _ in range(3):
            assert breaker.acquire() is None
            breaker.record_failure()
        assert breaker.state == "OPEN" and breaker.acquire() == pytest.approx(1.0)
        now[0] = 1.0
        assert breaker.acquire() is None       # the single trial request
        assert breaker.acquire() is not None   # nobody else while the trial runs
        breaker.record_success()
        assert breaker.state == "CLOSED" and changes == ["OPEN", "HALF_OPEN", "CLOSED"]

    def test_during_an_outage_stops_hammering_the_bank_and_saves_retry_budgets(self, harnesses):
        h = harnesses(breaker_failure_threshold=3, breaker_cooldown_ms=60_000, max_attempts=10)
        for _ in range(20):
            h.submit(new_payment(destinationAccount="EXT-DOWN-99"))
        h.start()
        time.sleep(1.5)
        assert h.platform.breaker.state == "OPEN"
        assert sum(h.bank.calls.values()) <= 5          # without a breaker: hundreds of calls
        assert h.query("SELECT count(*) AS n FROM payments WHERE status = 'FAILED'")[0]["n"] == 0
