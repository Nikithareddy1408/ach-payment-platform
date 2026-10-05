"""The payment worker: pulls jobs from the queue and drives each payment
through its lifecycle by calling the bank.

Each step is a short transaction, and the bank call happens OUTSIDE any
transaction (never hold database locks while waiting on the network). A crash
at any point leaves a consistent, recoverable state.
"""
import logging
import os
import socket
import time
import uuid

from psycopg_pool import ConnectionPool

from .bank import BankGateway, BankResult, CircuitBreaker
from .config import Settings
from .db import transaction
from .domain import COMPLETED, FAILED, PROCESSING, RETRYING, TERMINAL, backoff_ms
from .job_queue import ClaimedJob, claim_job, complete_job, holds_lease, requeue_job
from .loop import BackgroundLoop
from .metrics import Metrics
from .payments import Actor, PaymentService

log = logging.getLogger("ach.worker")


class PaymentWorker:
    def __init__(self, pool: ConnectionPool, settings: Settings, payments: PaymentService, bank: BankGateway,
                 breaker: CircuitBreaker, metrics: Metrics, instance_id: str | None = None):
        self.pool, self.settings, self.payments, self.bank, self.breaker, self.metrics = pool, settings, payments, bank, breaker, metrics
        self.instance_id = instance_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.loop = BackgroundLoop("payment-worker", self.process_next, settings.worker_concurrency, settings.poll_interval_ms / 1000)

    def start(self) -> None:
        self.loop.start()

    def stop(self) -> None:
        self.loop.stop()

    def process_next(self, thread_index: int = 0) -> bool:
        """Processes one job. Returns False when nothing is ready."""
        worker_id = f"{self.instance_id}#{thread_index}"
        job = claim_job(self.pool, worker_id, self.settings.job_lease_ms)
        if job is None:
            return False

        # Circuit open: put the job back WITHOUT spending one of its attempts.
        wait_s = self.breaker.acquire()
        if wait_s is not None:
            with self.pool.connection() as conn:
                requeue_job(conn, job.payment_id, int(wait_s * 1000), job.lease_token)
            return True

        actor = Actor(f"worker:{worker_id}", "req_" + uuid.uuid4().hex)
        payment = None
        try:
            payment = self._start_attempt(job, actor)  # Step 1: mark PROCESSING
        finally:
            if payment is None:
                self.breaker.release()
        if payment is None:
            return True

        # Step 2: call the bank, with the payment id as idempotency key.
        with _Timer(self.metrics) as timer:
            try:
                result = self.bank.submit_transfer(
                    idempotency_key=payment["id"], source_account=payment["source_account"],
                    destination_account=payment["destination_account"], amount_cents=payment["amount_cents"],
                    currency=payment["currency"], reference=payment["reference"], request_id=actor.request_id,
                )
            except Exception as error:  # a bug in the client must not lose the payment
                result = BankResult("retryable", "BANK_CLIENT_ERROR", str(error))
            timer.outcome = result.kind
        if result.kind == "retryable":
            self.breaker.record_failure()
        else:
            self.breaker.record_success()

        self._record_outcome(job, payment, result, actor)  # Step 3
        return True

    def _start_attempt(self, job: ClaimedJob, actor: Actor) -> dict | None:
        with transaction(self.pool) as conn:
            if not holds_lease(conn, job):
                return None
            current = conn.execute("SELECT * FROM payments WHERE id = %s FOR UPDATE", (job.payment_id,)).fetchone()
            if current is None or current["status"] in TERMINAL:
                complete_job(conn, job.payment_id)
                return None
            if current["status"] == PROCESSING:
                self.payments.transition(
                    conn, current["id"], RETRYING, actor=actor, metadata={"recoveredFromInterruptedAttempt": True},
                    reason="Previous attempt was interrupted before its result was recorded; "
                           "retrying safely with the same bank idempotency key",
                )
            attempt = current["attempts"] + 1
            return self.payments.transition(
                conn, current["id"], PROCESSING, actor=actor, attempts=attempt, metadata={"attempt": attempt},
                reason=f"Attempt {attempt} of {self.settings.max_attempts}: submitting transfer to bank",
            )

    def _record_outcome(self, job: ClaimedJob, payment: dict, result: BankResult, actor: Actor) -> None:
        attempt, pid = payment["attempts"], payment["id"]
        with transaction(self.pool) as conn:
            if not holds_lease(conn, job):
                log.warning("lease lost before recording outcome; the new lease holder will finish this payment",
                            extra={"payment_id": pid, "attempt": attempt})
                return

            if result.kind == "accepted":
                self.payments.transition(
                    conn, pid, COMPLETED, actor=actor, bank_transfer_id=result.transfer_id, last_error=None,
                    reason=f"Bank accepted transfer {result.transfer_id}",
                    metadata={"attempt": attempt, "bankTransferId": result.transfer_id, "idempotentReplayByBank": result.replayed},
                )
                complete_job(conn, pid)
                log.info("payment completed", extra={"payment_id": pid, "attempt": attempt})
                return

            error = {"code": result.code, "message": result.message}
            if result.kind == "rejected":
                self.payments.transition(conn, pid, FAILED, actor=actor, last_error=error,
                                         reason=f"Bank rejected the transfer: {result.code}",
                                         metadata={"attempt": attempt, "code": result.code, "permanent": True})
                complete_job(conn, pid)
            elif attempt >= self.settings.max_attempts:
                self.payments.transition(conn, pid, FAILED, actor=actor, last_error=error,
                                         reason=f"Gave up after {attempt} attempts; last error: {result.code}",
                                         metadata={"attempt": attempt, "code": result.code, "retriesExhausted": True})
                complete_job(conn, pid)
            else:
                delay = backoff_ms(attempt, self.settings.retry_base_ms, self.settings.retry_max_ms)
                self.payments.transition(conn, pid, RETRYING, actor=actor, last_error=error,
                                         reason=f"Attempt {attempt} failed ({result.code}); retrying in {delay / 1000:.1f}s",
                                         metadata={"attempt": attempt, "code": result.code, "retryInMs": delay})
                requeue_job(conn, pid, delay)
            log.info("payment attempt failed", extra={"payment_id": pid, "attempt": attempt, "code": result.code})


class _Timer:
    """Times the bank call and records it under the outcome label once known."""

    def __init__(self, metrics: Metrics):
        self.metrics, self.outcome = metrics, "error"

    def __enter__(self) -> "_Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> bool:
        self.metrics.bank_requests.labels(outcome=self.outcome).observe(time.perf_counter() - self._start)
        return False
