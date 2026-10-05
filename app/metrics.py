"""Prometheus metrics, scraped from GET /metrics. They answer the on-call
questions: Is the queue backing up? Is the bank failing? Are webhooks failing?"""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from psycopg_pool import ConnectionPool


class _DatabaseGauges:
    """Computed from the database at scrape time, so every instance reports the same truth."""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def collect(self):
        with self.pool.connection() as conn:
            jobs = conn.execute("SELECT state, count(*)::int AS n FROM payment_jobs WHERE state <> 'DONE' GROUP BY state").fetchall()
            pending = conn.execute(
                "SELECT count(*)::int AS n FROM webhook_deliveries WHERE state IN ('PENDING','DELIVERING')"
            ).fetchone()["n"]
            oldest = conn.execute(
                "SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0)::float8 AS age FROM payments "
                "WHERE status IN ('PENDING','PROCESSING','RETRYING')"
            ).fetchone()["age"]
        g = GaugeMetricFamily("ach_payment_jobs", "Payment jobs by queue state (a growing QUEUED backlog = workers can't keep up)", labels=["state"])
        for row in jobs:
            g.add_metric([row["state"]], row["n"])
        yield g
        yield GaugeMetricFamily("ach_webhook_deliveries_pending", "Webhook deliveries waiting to be sent", value=pending)
        yield GaugeMetricFamily("ach_oldest_open_payment_age_seconds", "Age of the oldest unfinished payment (alert if it keeps growing)", value=oldest)


class Metrics:
    def __init__(self, pool: ConnectionPool | None = None):
        r = self.registry = CollectorRegistry()
        self.payments_created = Counter("ach_payments_created", "Payments accepted by the API", registry=r)
        self.idempotent_replays = Counter("ach_idempotent_replays", "Duplicate submissions answered with the original payment", registry=r)
        self.transitions = Counter("ach_payment_transitions", "Payment status changes, by new status", ["to"], registry=r)
        self.bank_requests = Histogram(
            "ach_bank_request_duration_seconds", "Latency of calls to the bank, by outcome", ["outcome"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10), registry=r,
        )
        self.breaker_state = Gauge("ach_bank_circuit_state", "Bank circuit breaker: 0 closed, 1 half-open, 2 open", registry=r)
        self.webhook_attempts = Counter("ach_webhook_delivery_attempts", "Webhook delivery attempts, by result", ["result"], registry=r)
        self.http_requests = Histogram(
            "ach_http_request_duration_seconds", "API request latency", ["method", "route", "status"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1), registry=r,
        )
        if pool is not None:
            r.register(_DatabaseGauges(pool))
