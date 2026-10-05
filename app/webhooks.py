"""Webhooks: endpoint registration, the transactional outbox, signatures,
SSRF protection, and the dispatcher that delivers them.

Guarantees:
  - nothing is lost: deliveries are written in the SAME transaction as the status change
  - at-least-once: retried with backoff until delivered or max attempts
  - in order per payment and endpoint
  - safe with many dispatcher instances (FOR UPDATE SKIP LOCKED)
  - isolated: a broken customer endpoint never slows down payment processing
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from psycopg_pool import ConnectionPool

from .config import Settings
from .db import transaction
from .domain import ApiError, backoff_ms, new_id, not_found
from .loop import BackgroundLoop
from .mappers import to_api_payment
from .metrics import Metrics

log = logging.getLogger("ach.webhooks")
SIGNATURE_HEADER = "ACH-Signature"


# ── Outbox: called INSIDE the status-change transaction ─────────────────────
def enqueue_webhooks_in_tx(conn, payment: dict, event: dict) -> None:
    endpoints = conn.execute(
        "SELECT id FROM webhook_endpoints WHERE customer_id = %s AND enabled", (payment["customer_id"],)
    ).fetchall()
    if not endpoints:
        return
    payload = json.dumps({
        "id": f"evt_{event['id']}",
        "type": f"payment.{event['to_status'].lower()}",
        "sequence": event["id"],
        "createdAt": event["created_at"].isoformat(),
        "data": {"previousStatus": event["from_status"], "reason": event["reason"], "payment": to_api_payment(payment)},
    })
    for endpoint in endpoints:
        conn.execute(
            """INSERT INTO webhook_deliveries (endpoint_id, payment_id, event_id, payload, state)
               VALUES (%s, %s, %s, %s, 'PENDING') ON CONFLICT (endpoint_id, event_id) DO NOTHING""",
            (endpoint["id"], payment["id"], event["id"], payload),
        )


# ── Signatures (same scheme as Stripe) ──────────────────────────────────────
#   ACH-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>
# The timestamp is signed too, so an old message can't be replayed later.
def sign_payload(secret: str, payload: str, timestamp: int | None = None) -> str:
    t = int(time.time()) if timestamp is None else timestamp
    v1 = hmac.new(secret.encode(), f"{t}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


def verify_signature(secret: str, payload: str, header: str | None, tolerance_s: int = 300, now: int | None = None) -> bool:
    """For receivers: True only if the signature matches AND is recent (default 5 minutes)."""
    try:
        parts = dict(item.split("=", 1) for item in (header or "").split(","))
        t, provided = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - t) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{t}.{payload}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


# ── SSRF protection ─────────────────────────────────────────────────────────
# Without it, a customer could register http://169.254.169.254/... (cloud
# credentials) or http://10.0.0.5/admin, and our servers would make requests
# into our own private network for them.
class UnsafeUrl(Exception):
    pass


def is_private_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.strip("[]"))
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def _resolve(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except socket.gaierror as error:
        raise UnsafeUrl(f"host could not be resolved ({error})") from None


def check_webhook_url(url: str, *, block_private_ips: bool, require_https: bool) -> list[str]:
    """Raises UnsafeUrl if not allowed. Returns the vetted IP addresses (empty if not checked)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeUrl("must be an absolute http(s) URL")
    if require_https and parts.scheme != "https":
        raise UnsafeUrl("must use https")
    if parts.username or parts.password:
        raise UnsafeUrl("must not contain credentials")
    if not block_private_ips:
        return []
    host = parts.hostname
    if host == "localhost" or host.endswith((".localhost", ".internal")):
        raise UnsafeUrl("must not point to an internal host")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = _resolve(host)
    if any(is_private_address(a) for a in addresses):
        raise UnsafeUrl("must not point to a private or internal network address")
    return addresses


# ── Endpoint management ─────────────────────────────────────────────────────
class WebhookEndpointService:
    def __init__(self, pool: ConnectionPool, settings: Settings):
        self.pool, self.settings = pool, settings

    def create(self, customer_id: str, url: str, description: str | None) -> dict:
        try:
            check_webhook_url(url, block_private_ips=self.settings.webhook_block_private_ips,
                              require_https=self.settings.environment == "production")
        except UnsafeUrl as error:
            raise ApiError(400, "VALIDATION_ERROR", "The webhook URL is not allowed.", [{"field": "url", "message": str(error)}])
        with self.pool.connection() as conn:
            return conn.execute(
                "INSERT INTO webhook_endpoints (id, customer_id, url, secret, description) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (new_id("whe"), customer_id, url, "whsec_" + secrets.token_hex(32), description),
            ).fetchone()

    def list(self, customer_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM webhook_endpoints WHERE customer_id = %s ORDER BY created_at DESC", (customer_id,)).fetchall()

    def disable(self, customer_id: str, endpoint_id: str) -> dict:
        """Disables an endpoint and cancels its undelivered events."""
        with transaction(self.pool) as conn:
            row = conn.execute(
                """UPDATE webhook_endpoints SET enabled = false, disabled_at = COALESCE(disabled_at, now())
                   WHERE id = %s AND customer_id = %s RETURNING *""", (endpoint_id, customer_id),
            ).fetchone()
            if not row:
                raise not_found("WEBHOOK_ENDPOINT", endpoint_id)
            conn.execute(
                """UPDATE webhook_deliveries SET state = 'FAILED', last_error = 'endpoint disabled', lease_expires_at = NULL
                   WHERE endpoint_id = %s AND state IN ('PENDING', 'DELIVERING')""", (endpoint_id,),
            )
            return row

    def retry_delivery(self, customer_id: str, delivery_id: int) -> dict:
        """Re-sends a delivery that gave up (e.g. after the customer fixed their server)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """UPDATE webhook_deliveries d
                   SET state = 'PENDING', attempts = 0, next_attempt_at = now(), last_error = NULL, lease_expires_at = NULL
                   FROM webhook_endpoints e
                   WHERE d.id = %s AND e.id = d.endpoint_id AND e.customer_id = %s AND e.enabled AND d.state = 'FAILED'
                   RETURNING d.id, d.state""", (delivery_id, customer_id),
            ).fetchone()
            if row:
                return row
            exists = conn.execute(
                "SELECT 1 FROM webhook_deliveries d JOIN webhook_endpoints e ON e.id = d.endpoint_id WHERE d.id = %s AND e.customer_id = %s",
                (delivery_id, customer_id),
            ).fetchone()
        if not exists:
            raise not_found("WEBHOOK_DELIVERY", str(delivery_id))
        raise ApiError(409, "DELIVERY_NOT_RETRYABLE", "Only FAILED deliveries to enabled endpoints can be retried.")


def to_api_endpoint(e: dict, include_secret: bool = False) -> dict:
    out = {"id": e["id"], "url": e["url"], "description": e["description"], "enabled": e["enabled"], "createdAt": e["created_at"].isoformat()}
    if include_secret:
        out["secret"] = e["secret"]
    return out


# ── Dispatcher ──────────────────────────────────────────────────────────────
_CLAIM = """
WITH next AS (
  SELECT d.id FROM webhook_deliveries d
  WHERE ((d.state = 'PENDING' AND d.next_attempt_at <= now())
      OR (d.state = 'DELIVERING' AND d.lease_expires_at < now()))
    AND NOT EXISTS (                      -- keep events in order per payment + endpoint
      SELECT 1 FROM webhook_deliveries e
      WHERE e.endpoint_id = d.endpoint_id AND e.payment_id = d.payment_id
        AND e.id < d.id AND e.state IN ('PENDING', 'DELIVERING'))
  ORDER BY d.next_attempt_at, d.id
  LIMIT 1
  FOR UPDATE SKIP LOCKED
), claimed AS (
  UPDATE webhook_deliveries d
  SET state = 'DELIVERING', attempts = d.attempts + 1,
      lease_expires_at = now() + make_interval(secs => %s::float8 / 1000)
  FROM next WHERE d.id = next.id
  RETURNING d.id, d.endpoint_id, d.event_id, d.payload, d.attempts
)
SELECT c.*, e.url, e.secret, e.enabled FROM claimed c JOIN webhook_endpoints e ON e.id = c.endpoint_id
"""


class WebhookDispatcher:
    def __init__(self, pool: ConnectionPool, settings: Settings, metrics: Metrics):
        self.pool, self.settings, self.metrics = pool, settings, metrics
        self.http = httpx.Client(timeout=settings.webhook_timeout_ms / 1000, follow_redirects=False)
        self.loop = BackgroundLoop("webhook-dispatcher", lambda _i: self.deliver_next(),
                                   settings.webhook_concurrency, settings.poll_interval_ms / 1000)

    def start(self) -> None:
        self.loop.start()

    def stop(self) -> None:
        self.loop.stop()
        self.http.close()

    def deliver_next(self) -> bool:
        """Delivers one webhook. Returns False when nothing is due."""
        with self.pool.connection() as conn:
            d = conn.execute(_CLAIM, (self.settings.webhook_lease_ms,)).fetchone()
        if d is None:
            return False
        if not d["enabled"]:
            self._finish(d, "FAILED", error="endpoint disabled")
            return True

        status_code, error = None, None
        try:
            status_code = self._send(d)
            if not 200 <= status_code < 300:
                error = f"HTTP {status_code}"
        except httpx.TimeoutException:
            error = "timeout"
        except (httpx.HTTPError, UnsafeUrl, OSError) as exc:
            error = str(exc) or type(exc).__name__

        if error is None:
            self.metrics.webhook_attempts.labels(result="delivered").inc()
            self._finish(d, "DELIVERED", status_code=status_code)
        elif d["attempts"] >= self.settings.webhook_max_attempts:
            self.metrics.webhook_attempts.labels(result="gave_up").inc()
            log.warning("webhook delivery gave up", extra={"delivery_id": d["id"], "endpoint_id": d["endpoint_id"], "reason": error})
            self._finish(d, "FAILED", status_code=status_code, error=error)
        else:
            self.metrics.webhook_attempts.labels(result="retry").inc()
            delay = backoff_ms(d["attempts"], self.settings.webhook_retry_base_ms, self.settings.webhook_retry_max_ms)
            self._finish(d, "PENDING", status_code=status_code, error=error, retry_in_ms=delay)
        return True

    def _send(self, d: dict) -> int:
        url = d["url"]
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ach-payment-platform-webhooks/1.0",
            "ACH-Event-Id": f"evt_{d['event_id']}",
            "ACH-Delivery-Attempt": str(d["attempts"]),
            SIGNATURE_HEADER: sign_payload(d["secret"], d["payload"]),
        }
        extensions = {}
        addresses = check_webhook_url(url, block_private_ips=self.settings.webhook_block_private_ips,
                                      require_https=self.settings.environment == "production")
        if addresses:
            # Connect to the exact IP we just vetted (not a fresh DNS lookup), so a
            # DNS answer that changes between check and connect can't sneak us into
            # a private network ("DNS rebinding"). TLS still verifies the real hostname.
            parts = urlsplit(url)
            ip = addresses[0]
            host_for_url = f"[{ip}]" if ":" in ip else ip
            netloc = host_for_url + (f":{parts.port}" if parts.port else "")
            headers["Host"] = parts.netloc
            extensions["sni_hostname"] = parts.hostname
            url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
        response = self.http.post(url, content=d["payload"], headers=headers, extensions=extensions)
        return response.status_code

    def _finish(self, d: dict, state: str, *, status_code: int | None = None, error: str | None = None, retry_in_ms: int = 0) -> None:
        # "attempts = %s" is a fencing token: if our lease expired and another dispatcher
        # re-claimed this row, attempts changed and this update does nothing.
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE webhook_deliveries SET
                     state = %(state)s, last_status_code = %(code)s, last_error = %(error)s, lease_expires_at = NULL,
                     delivered_at = CASE WHEN %(state)s = 'DELIVERED' THEN now() ELSE delivered_at END,
                     next_attempt_at = CASE WHEN %(state)s = 'PENDING'
                                       THEN now() + make_interval(secs => %(delay)s::float8 / 1000) ELSE next_attempt_at END
                   WHERE id = %(id)s AND attempts = %(attempts)s AND state = 'DELIVERING'""",
                {"state": state, "code": status_code, "error": error, "delay": retry_in_ms, "id": d["id"], "attempts": d["attempts"]},
            )
