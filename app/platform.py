"""Composition root: builds every component and wires them together.
The same function is used by the real server, the tests, and the demo."""
import logging

from .api import create_api
from .bank import BankGateway, CircuitBreaker, HttpBankGateway
from .config import Settings
from .db import create_pool
from .metrics import Metrics
from .payments import PaymentService
from .webhooks import WebhookDispatcher, WebhookEndpointService
from .worker import PaymentWorker

log = logging.getLogger("ach.platform")


class Platform:
    def __init__(self, settings: Settings, bank: BankGateway | None = None):
        self.settings = settings
        self.pool = create_pool(settings.database_url, settings.database_pool_size)
        self.metrics = Metrics(self.pool)
        self.payments = PaymentService(self.pool, settings, self.metrics)
        self.webhook_endpoints = WebhookEndpointService(self.pool, settings)
        self.bank = bank or HttpBankGateway(settings.bank_base_url, settings.bank_timeout_ms)

        def on_breaker_change(state: str) -> None:
            self.metrics.breaker_state.set({"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}[state])
            log.warning("bank circuit breaker changed state", extra={"state": state})

        self.breaker = CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_cooldown_ms, on_breaker_change)
        self.worker = PaymentWorker(self.pool, settings, self.payments, self.bank, self.breaker, self.metrics)
        self.dispatcher = WebhookDispatcher(self.pool, settings, self.metrics)
        self.api = create_api(self)
        self._closed = False

    def close(self) -> None:
        """Graceful shutdown: let in-flight work finish, then disconnect. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self.worker.stop()
        self.dispatcher.stop()
        if hasattr(self.bank, "close"):
            self.bank.close()
        self.pool.close()
