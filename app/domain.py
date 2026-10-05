"""Core business rules with no I/O: payment statuses, money, retry timing, errors."""
import random
import re
import uuid
from decimal import Decimal, InvalidOperation

# ── Payment lifecycle (mirrored by the payments_guard trigger in the database) ──
#
#   PENDING ──► PROCESSING ──► COMPLETED
#                 │    ▲  └──► FAILED
#                 ▼    │
#                RETRYING
PENDING, PROCESSING, COMPLETED, FAILED, RETRYING = "PENDING", "PROCESSING", "COMPLETED", "FAILED", "RETRYING"
STATUSES = (PENDING, PROCESSING, COMPLETED, FAILED, RETRYING)
TERMINAL = {COMPLETED, FAILED}

TRANSITIONS: dict[str, set[str]] = {
    PENDING: {PROCESSING},
    PROCESSING: {COMPLETED, FAILED, RETRYING},
    RETRYING: {PROCESSING},
    COMPLETED: set(),
    FAILED: set(),
}


class IllegalTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"Illegal payment status transition {current} -> {target}")


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS[current]


def assert_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(current, target)


# ── Money: always integer cents, never floating point ──
_AMOUNT_TEXT = re.compile(r"^\d{1,13}(\.\d{1,2})?$")


def to_cents(amount: object) -> int | None:
    """250, 250.5, "250.00" → cents. None if not a valid amount with at most 2 decimals."""
    if isinstance(amount, bool):
        return None
    if isinstance(amount, str):
        if not _AMOUNT_TEXT.match(amount):
            return None
        value = Decimal(amount)
    elif isinstance(amount, (int, float)):
        try:
            value = Decimal(str(amount))  # str() avoids binary float artifacts
        except InvalidOperation:
            return None
        if not value.is_finite():
            return None
    else:
        return None
    cents = value * 100
    if cents != cents.to_integral_value():
        return None
    return int(cents)


def format_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def mask_account(account: str) -> str:
    return "****" + account[-4:]


# ── Retry timing ──
def backoff_ms(attempt: int, base_ms: int, max_ms: int, rand: float | None = None) -> int:
    """Exponential backoff with jitter: about base, 2x, 4x, ... capped at max, randomized by up to 50%."""
    ceiling = min(max_ms, base_ms * 2 ** max(0, attempt - 1))
    r = random.random() if rand is None else rand
    return round(ceiling / 2 + r * ceiling / 2)


# ── Ids and errors ──
def new_id(prefix: str) -> str:
    """Prefixed, unguessable ids (pay_..., whe_..., key_...). The prefix shows what an id refers to."""
    return f"{prefix}_{uuid.uuid4().hex}"


class ApiError(Exception):
    """An error returned to the API client exactly as described."""

    def __init__(self, status_code: int, code: str, message: str, details: object = None):
        super().__init__(message)
        self.status_code, self.code, self.message, self.details = status_code, code, message, details


def not_found(what: str, ident: str) -> ApiError:
    return ApiError(404, f"{what.upper()}_NOT_FOUND", f"No {what.lower().replace('_', ' ')} with id '{ident}'.")
