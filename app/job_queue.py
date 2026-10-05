"""Durable job queue in PostgreSQL, claimed with FOR UPDATE SKIP LOCKED.

SKIP LOCKED lets many workers (on many machines) pull from the same table at
once: each skips rows another worker has locked, so a job is never handed to
two workers. Each claim gets a lease (expiry time) and a random token. If a
worker dies, the lease expires and the job is claimed again. If a slow
"zombie" worker comes back, its token no longer matches, so it cannot
overwrite the result.
"""
from dataclasses import dataclass

from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class ClaimedJob:
    payment_id: str
    lease_token: str
    recovered: bool  # True if the previous holder's lease expired


def claim_job(pool: ConnectionPool, worker_id: str, lease_ms: int) -> ClaimedJob | None:
    with pool.connection() as conn:
        row = conn.execute(
            """WITH next AS (
                 SELECT payment_id, state AS prev_state FROM payment_jobs
                 WHERE (state = 'QUEUED' AND run_at <= now())
                    OR (state = 'RUNNING' AND lease_expires_at < now())
                 ORDER BY run_at
                 LIMIT 1
                 FOR UPDATE SKIP LOCKED
               )
               UPDATE payment_jobs j
               SET state = 'RUNNING', lease_token = gen_random_uuid(), locked_by = %s,
                   lease_expires_at = now() + make_interval(secs => %s::float8 / 1000),
                   claims = j.claims + 1, updated_at = now()
               FROM next WHERE j.payment_id = next.payment_id
               RETURNING j.payment_id, j.lease_token::text AS lease_token, next.prev_state""",
            (worker_id, lease_ms),
        ).fetchone()
    if row is None:
        return None
    return ClaimedJob(row["payment_id"], row["lease_token"], row["prev_state"] == "RUNNING")


def holds_lease(conn, job: ClaimedJob) -> bool:
    """Locks the job row and checks we still own it. Call inside the transaction that records the outcome."""
    row = conn.execute(
        "SELECT state = 'RUNNING' AND lease_token::text = %s AS ok FROM payment_jobs WHERE payment_id = %s FOR UPDATE",
        (job.lease_token, job.payment_id),
    ).fetchone()
    return bool(row and row["ok"])


def complete_job(conn, payment_id: str) -> None:
    conn.execute(
        "UPDATE payment_jobs SET state = 'DONE', lease_token = NULL, lease_expires_at = NULL, locked_by = NULL, updated_at = now() "
        "WHERE payment_id = %s", (payment_id,),
    )


def requeue_job(conn, payment_id: str, delay_ms: int, lease_token: str | None = None) -> None:
    conn.execute(
        """UPDATE payment_jobs SET state = 'QUEUED', run_at = now() + make_interval(secs => %s::float8 / 1000),
                  lease_token = NULL, lease_expires_at = NULL, locked_by = NULL, updated_at = now()
           WHERE payment_id = %s AND (%s::text IS NULL OR lease_token::text = %s::text)""",
        (delay_ms, payment_id, lease_token, lease_token),
    )
