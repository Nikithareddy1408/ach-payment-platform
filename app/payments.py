"""Payment business logic: accepting payments (with duplicate protection) and
the single function through which EVERY status change happens."""
import base64
import hashlib
import json
import re
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import Settings
from .db import is_unique_violation, transaction
from .domain import PENDING, TERMINAL, ApiError, assert_transition, format_cents, new_id, to_cents
from .mappers import to_api_event, to_api_payment  # noqa: F401  (re-exported)
from .metrics import Metrics
from .webhooks import enqueue_webhooks_in_tx

IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7E]{8,255}$")  # 8-255 printable ASCII characters, no spaces
_UNSET = object()


@dataclass
class Actor:
    """Who caused a change, for the audit trail."""
    actor: str  # "api" | "worker:<id>" | "system"
    request_id: str | None = None


@dataclass
class NewPayment:
    customer_id: str
    source_account: str
    destination_account: str
    amount: object
    reference: str


class PaymentService:
    def __init__(self, pool: ConnectionPool, settings: Settings, metrics: Metrics):
        self.pool, self.settings, self.metrics = pool, settings, metrics

    # ── Submission ────────────────────────────────────────────────────────
    def create(self, auth_customer_id: str, p: NewPayment, idempotency_key: str | None, actor: Actor) -> tuple[dict, bool]:
        """Accepts a payment for asynchronous processing. Returns (payment row, replayed).

        Duplicate protection has two layers, both backed by UNIQUE constraints:
          1. Idempotency-Key: same key + same body -> the original payment is returned.
          2. (customer, reference): the same payment reference can never be paid twice.
        """
        if not idempotency_key or not IDEMPOTENCY_KEY.match(idempotency_key):
            raise ApiError(
                400, "IDEMPOTENCY_KEY_REQUIRED",
                "Send an Idempotency-Key header (8-255 printable characters, no spaces) that is unique per payment. "
                "Reuse it only when retrying the same request.",
            )
        if p.customer_id != auth_customer_id:
            raise ApiError(403, "CUSTOMER_MISMATCH", "customerId does not match the authenticated API key.")

        errors = []
        cents = to_cents(p.amount)
        if cents is None:
            errors.append({"field": "amount", "message": "Must be a number with at most 2 decimal places, e.g. 250.00."})
        elif cents <= 0:
            errors.append({"field": "amount", "message": "Must be greater than 0."})
        elif cents > self.settings.max_amount_cents:
            errors.append({"field": "amount", "message": f"Must not exceed {format_cents(self.settings.max_amount_cents)}."})
        if p.source_account == p.destination_account:
            errors.append({"field": "destinationAccount", "message": "Must be different from sourceAccount."})
        if errors:
            raise ApiError(400, "VALIDATION_ERROR", "The payment request is invalid.", errors)

        request_hash = hashlib.sha256(
            json.dumps([p.customer_id, p.source_account, p.destination_account, cents, p.reference]).encode()
        ).hexdigest()

        # A concurrent identical request can win the race to insert. We then lose with a
        # unique violation, and the second pass finds and returns the winner's payment.
        for attempt in (1, 2):
            try:
                with transaction(self.pool) as conn:
                    payment, replayed = self._create_in_tx(conn, p, cents, idempotency_key, request_hash, actor)
                (self.metrics.idempotent_replays if replayed else self.metrics.payments_created).inc()
                return payment, replayed
            except psycopg.Error as error:
                if attempt == 1 and is_unique_violation(error):
                    continue
                raise
        raise AssertionError("unreachable")

    def _create_in_tx(self, conn, p: NewPayment, cents: int, key: str, request_hash: str, actor: Actor) -> tuple[dict, bool]:
        # ONE statement checks both the key and the reference, so both checks see the same
        # snapshot of committed data. Two separate SELECTs can straddle another request's
        # commit: the key check misses, the reference check hits, and a legitimate retry
        # wrongly gets 409. (Found by the evals: see test_duplicates_and_async.py.)
        matches = conn.execute(
            "SELECT * FROM payments WHERE customer_id = %s AND (idempotency_key = %s OR reference = %s)",
            (p.customer_id, key, p.reference),
        ).fetchall()
        existing = next((m for m in matches if m["idempotency_key"] == key), None)
        if existing:
            if existing["request_hash"] != request_hash:
                raise ApiError(422, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used with a different request.",
                               {"paymentId": existing["id"]})
            return existing, True
        same_reference = next((m for m in matches if m["reference"] == p.reference), None)
        if same_reference:
            raise ApiError(409, "DUPLICATE_REFERENCE", f"A payment with reference '{p.reference}' already exists for this customer.",
                           {"existingPaymentId": same_reference["id"]})

        payment = conn.execute(
            """INSERT INTO payments (id, customer_id, source_account, destination_account, amount_cents, currency,
                                     reference, status, idempotency_key, request_hash)
               VALUES (%s, %s, %s, %s, %s, 'USD', %s, 'PENDING', %s, %s) RETURNING *""",
            (new_id("pay"), p.customer_id, p.source_account, p.destination_account, cents, p.reference, key, request_hash),
        ).fetchone()
        self._record_event(conn, payment, None, PENDING, "Payment accepted and queued for processing", actor, {})
        # Same transaction: a payment can never exist without its job (or its first audit event).
        conn.execute("INSERT INTO payment_jobs (payment_id, state) VALUES (%s, 'QUEUED')", (payment["id"],))
        return payment, False

    # ── The ONLY way a status ever changes ────────────────────────────────
    def transition(self, conn, payment_id: str, target: str, *, reason: str, actor: Actor, attempts: int | None = None,
                   bank_transfer_id: str | None = None, last_error=_UNSET, metadata: dict | None = None) -> dict:
        """In the caller's transaction: lock the payment, check the change is legal,
        update it, append an audit event, and queue webhooks for it."""
        before = conn.execute("SELECT * FROM payments WHERE id = %s FOR UPDATE", (payment_id,)).fetchone()
        if before is None:
            raise LookupError(f"Payment {payment_id} not found")
        assert_transition(before["status"], target)

        set_error = last_error is not _UNSET
        error = last_error if set_error and last_error else {}
        after = conn.execute(
            """UPDATE payments SET
                 status = %(target)s,
                 attempts = COALESCE(%(attempts)s, attempts),
                 bank_transfer_id = COALESCE(%(transfer)s, bank_transfer_id),
                 last_error_code = CASE WHEN %(set_error)s THEN %(code)s ELSE last_error_code END,
                 last_error_message = CASE WHEN %(set_error)s THEN %(message)s ELSE last_error_message END,
                 completed_at = CASE WHEN %(terminal)s THEN now() ELSE completed_at END
               WHERE id = %(id)s RETURNING *""",
            {"id": payment_id, "target": target, "attempts": attempts, "transfer": bank_transfer_id, "set_error": set_error,
             "code": error.get("code"), "message": error.get("message"), "terminal": target in TERMINAL},
        ).fetchone()
        self._record_event(conn, after, before["status"], target, reason, actor, metadata or {})
        self.metrics.transitions.labels(to=target).inc()
        return after

    def _record_event(self, conn, payment: dict, from_status: str | None, to_status: str, reason: str, actor: Actor, metadata: dict) -> None:
        event = conn.execute(
            """INSERT INTO payment_events (payment_id, from_status, to_status, reason, actor, request_id, metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (payment["id"], from_status, to_status, reason, actor.actor, actor.request_id, Jsonb(metadata)),
        ).fetchone()
        enqueue_webhooks_in_tx(conn, payment, event)

    # ── Reads ─────────────────────────────────────────────────────────────
    def get(self, customer_id: str, payment_id: str) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM payments WHERE id = %s AND customer_id = %s", (payment_id, customer_id)).fetchone()

    def events(self, payment_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM payment_events WHERE payment_id = %s ORDER BY id", (payment_id,)).fetchall()

    def list(self, customer_id: str, *, status: str | None, reference: str | None, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
        """Newest first, keyset-paginated (stable even while new payments arrive)."""
        where, params = ["customer_id = %s"], [customer_id]
        if status:
            where.append("status = %s")
            params.append(status)
        if reference:
            where.append("reference = %s")
            params.append(reference)
        with self.pool.connection() as conn:
            if cursor:
                cursor_id = _decode_cursor(cursor)
                if not conn.execute("SELECT 1 FROM payments WHERE id = %s AND customer_id = %s", (cursor_id, customer_id)).fetchone():
                    raise ApiError(400, "INVALID_CURSOR", "The cursor is not valid.")
                # Compared inside the database so timestamp precision (microseconds) is exact.
                where.append("(created_at, id) < (SELECT created_at, id FROM payments WHERE id = %s)")
                params.append(cursor_id)
            rows = conn.execute(
                f"SELECT * FROM payments WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT %s",
                (*params, limit + 1),
            ).fetchall()
        page = rows[:limit]
        next_cursor = base64.urlsafe_b64encode(page[-1]["id"].encode()).decode() if len(rows) > limit and page else None
        return page, next_cursor


def _decode_cursor(cursor: str) -> str:
    try:
        value = base64.urlsafe_b64decode(cursor.encode()).decode()
    except Exception:
        value = ""
    if not re.fullmatch(r"pay_[0-9a-f]{32}", value):
        raise ApiError(400, "INVALID_CURSOR", "The cursor is not valid.")
    return value
