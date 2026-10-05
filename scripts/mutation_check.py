""""Evals of the evals": deliberately inject realistic bugs, one at a time, and
confirm the test suite FAILS for each. A suite that still passes with a bug
inside is giving false confidence.

    python scripts/mutation_check.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MUTATIONS = [
    ("Worker sends a NEW bank idempotency key on every attempt (the classic double-payment bug)",
     "app/worker.py", 'idempotency_key=payment["id"],', 'idempotency_key=f"{payment[\'id\']}-{payment[\'attempts\']}",',
     "tests/test_03_lifecycle_and_resilience.py"),
    ("Idempotent replay disabled (retries create new payments)",
     "app/payments.py", 'existing = next((m for m in matches if m["idempotency_key"] == key), None)', "existing = None",
     "tests/test_02_duplicates_and_async.py"),
    ("Duplicate lookup split into two statements (the race condition found during development)",
     "app/payments.py",
     '''        matches = conn.execute(
            "SELECT * FROM payments WHERE customer_id = %s AND (idempotency_key = %s OR reference = %s)",
            (p.customer_id, key, p.reference),
        ).fetchall()
        existing = next((m for m in matches if m["idempotency_key"] == key), None)''',
     '''        by_key = conn.execute("SELECT * FROM payments WHERE customer_id = %s AND idempotency_key = %s", (p.customer_id, key)).fetchall()
        matches = conn.execute("SELECT * FROM payments WHERE customer_id = %s AND reference = %s", (p.customer_id, p.reference)).fetchall()
        existing = by_key[0] if by_key else None''',
     "tests/test_02_duplicates_and_async.py"),
    ("Permanent bank rejections treated as retryable",
     "app/bank.py", 'return BankResult("rejected", code, message)', 'return BankResult("retryable", code, message)',
     "tests/test_03_lifecycle_and_resilience.py"),
    ("Lease check removed (zombie workers can overwrite results)",
     "app/job_queue.py", 'return bool(row and row["ok"])', "return row is not None",
     "tests/test_03_lifecycle_and_resilience.py"),
    ("Circuit breaker gate ignored",
     "app/worker.py", "if wait_s is not None:", "if wait_s is not None and False:",
     "tests/test_03_lifecycle_and_resilience.py"),
    ("Webhooks not written to the outbox",
     "app/payments.py", "enqueue_webhooks_in_tx(conn, payment, event)", "pass",
     "tests/test_04_status_audit_webhooks.py"),
    ("Webhook ordering guard removed",
     "app/webhooks.py", "AND e.id < d.id AND e.state IN ('PENDING', 'DELIVERING'))", "AND false)",
     "tests/test_04_status_audit_webhooks.py tests/test_05_chaos.py"),
    ("Database no longer blocks illegal status transitions",
     "migrations/001_init.sql", "IF NEW.status IS DISTINCT FROM OLD.status AND NOT (", "IF false AND NOT (",
     "tests/test_03_lifecycle_and_resilience.py"),
    ("Customers can read each other's payments",
     "app/payments.py", '"SELECT * FROM payments WHERE id = %s AND customer_id = %s", (payment_id, customer_id)',
     '"SELECT * FROM payments WHERE id = %s AND %s IS NOT NULL", (payment_id, customer_id)',
     "tests/test_01_submission.py"),
]

# Optional: run a subset, e.g.  python scripts/mutation_check.py 1 2 3
selected = {int(a) for a in sys.argv[1:]} or set(range(1, len(MUTATIONS) + 1))
caught = 0
for i, (name, file, find, replace, tests) in enumerate(MUTATIONS, 1):
    if i not in selected:
        continue
    path = ROOT / file
    original = path.read_text()
    if find not in original:
        print(f"✗ [{i}] mutation target not found in {file}: {name}")
        sys.exit(1)
    path.write_text(original.replace(find, replace, 1))
    try:
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", *tests.split()], cwd=ROOT, capture_output=True)
        failed = result.returncode != 0
    finally:
        path.write_text(original)  # always restore the real code
    caught += failed
    print(f"{'✓ caught ' if failed else '✗ MISSED '} [{i}/{len(MUTATIONS)}] {name}", flush=True)

print(f"\n{caught}/{len(selected)} injected bugs were caught by the evals.")
sys.exit(0 if caught == len(selected) else 1)
