"""The boundary to the banking partner, plus a circuit breaker.

Every bank outcome is sorted into one of three kinds, because each needs a
different reaction:
  accepted  -> money moved          -> COMPLETED
  rejected  -> retrying won't help  -> FAILED now (closed account, invalid data)
  retryable -> temporary problem    -> RETRYING with backoff (5xx, 429, timeout, network)

The payment id is always sent as the bank's Idempotency-Key. A timeout means
"we don't know whether the money moved"; retrying with the same key makes the
bank return the original result instead of moving the money twice.
"""
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

import httpx


@dataclass(frozen=True)
class BankResult:
    kind: Literal["accepted", "rejected", "retryable"]
    code: str = ""
    message: str = ""
    transfer_id: str | None = None
    replayed: bool = False


class BankGateway(Protocol):
    def submit_transfer(self, *, idempotency_key: str, source_account: str, destination_account: str,
                        amount_cents: int, currency: str, reference: str, request_id: str) -> BankResult: ...


class HttpBankGateway:
    def __init__(self, base_url: str, timeout_ms: int):
        self.client = httpx.Client(base_url=base_url, timeout=timeout_ms / 1000)
        self.timeout_ms = timeout_ms

    def submit_transfer(self, *, idempotency_key, source_account, destination_account, amount_cents, currency, reference, request_id) -> BankResult:
        try:
            response = self.client.post(
                "/v1/transfers",
                headers={"Idempotency-Key": idempotency_key, "X-Request-Id": request_id},
                json={"sourceAccount": source_account, "destinationAccount": destination_account,
                      "amountCents": amount_cents, "currency": currency, "reference": reference},
            )
        except httpx.TimeoutException:
            return BankResult("retryable", "BANK_TIMEOUT", f"No response within {self.timeout_ms}ms; outcome unknown")
        except httpx.HTTPError as error:
            return BankResult("retryable", "BANK_UNREACHABLE", str(error) or type(error).__name__)

        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.is_success and isinstance(data.get("transferId"), str):
            return BankResult("accepted", transfer_id=data["transferId"], replayed=bool(data.get("replayed")))
        code = data.get("code") or f"HTTP_{response.status_code}"
        message = data.get("message") or f"Bank responded with HTTP {response.status_code}"
        if response.is_success or response.status_code in (408, 409, 429) or response.status_code >= 500:
            return BankResult("retryable", code, message)
        return BankResult("rejected", code, message)

    def close(self) -> None:
        self.client.close()


class CircuitBreaker:
    """If the bank fails many times in a row, stop calling it for a cooldown
    instead of hammering a struggling system (and burning every payment's retry
    budget during an outage). After the cooldown, ONE trial request goes through
    ("half-open"): success closes the circuit, failure opens it again.

        CLOSED --(N failures in a row)--> OPEN --(cooldown)--> HALF_OPEN
          ^                                                       |
          +------------------(trial succeeds)---------------------+
    """

    def __init__(self, failure_threshold: int, cooldown_ms: int,
                 on_change: Callable[[str], None] = lambda s: None, clock: Callable[[], float] = time.monotonic):
        self.failure_threshold, self.cooldown_s = failure_threshold, cooldown_ms / 1000
        self.on_change, self.clock = on_change, clock
        self.state, self._failures, self._opened_at, self._trial_in_flight = "CLOSED", 0, 0.0, False
        self._lock = threading.Lock()

    def acquire(self) -> float | None:
        """Ask before calling the bank. Returns None if allowed, else seconds to wait."""
        with self._lock:
            if self.state == "OPEN":
                remaining = self._opened_at + self.cooldown_s - self.clock()
                if remaining > 0:
                    return remaining
                self._set("HALF_OPEN")
            if self.state == "HALF_OPEN":
                if self._trial_in_flight:
                    return max(1.0, self.cooldown_s / 10)
                self._trial_in_flight = True
            return None

    def release(self) -> None:
        """Gives back an unused permission (e.g. the job turned out to be finished already)."""
        with self._lock:
            self._trial_in_flight = False

    def record_success(self) -> None:
        """The bank responded (accepted OR a business rejection): it is up."""
        with self._lock:
            self._failures, self._trial_in_flight = 0, False
            if self.state != "CLOSED":
                self._set("CLOSED")

    def record_failure(self) -> None:
        """The bank failed in a retryable way (5xx, timeout, unreachable)."""
        with self._lock:
            self._trial_in_flight = False
            self._failures += 1
            if self.state == "HALF_OPEN" or self._failures >= self.failure_threshold:
                self._opened_at = self.clock()
                self._set("OPEN")

    def _set(self, state: str) -> None:
        self.state = state
        self.on_change(state)
