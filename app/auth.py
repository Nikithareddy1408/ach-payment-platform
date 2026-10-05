"""API keys. Only a SHA-256 hash is stored, so a database leak does not leak
usable keys. Each key belongs to one customer; every request is limited to
that customer's own data."""
import hashlib
import re
import secrets

from psycopg_pool import ConnectionPool

from .domain import new_id

_BEARER = re.compile(r"^Bearer\s+(sk_[A-Za-z0-9_-]{16,128})$")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_customer_with_api_key(pool: ConnectionPool, customer_id: str, name: str) -> dict:
    api_key = "sk_" + secrets.token_urlsafe(24)
    key_id = new_id("key")
    with pool.connection() as conn:
        conn.execute("INSERT INTO customers (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (customer_id, name))
        conn.execute(
            "INSERT INTO api_keys (id, customer_id, key_hash, key_prefix) VALUES (%s, %s, %s, %s)",
            (key_id, customer_id, _hash(api_key), api_key[:7]),
        )
    return {"customer_id": customer_id, "api_key": api_key, "key_id": key_id}


def authenticate(pool: ConnectionPool, authorization: str | None) -> str | None:
    """Returns the customer id for a valid 'Bearer sk_...' header, else None."""
    match = _BEARER.match(authorization or "")
    if not match:
        return None
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT customer_id FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL", (_hash(match.group(1)),)
        ).fetchone()
    return row["customer_id"] if row else None


def revoke_api_key(pool: ConnectionPool, key_id: str) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (key_id,))
