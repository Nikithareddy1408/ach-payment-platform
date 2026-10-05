# ACH Payment Platform

A production-grade backend service that accepts ACH payment requests over an API and processes them asynchronously through a banking partner. It is built to stay correct when the bank is down, the network times out, a client sends the same request twice, or a server crashes in the middle of a payment.

**Stack:** Python 3.12 · FastAPI · PostgreSQL 16 · pytest · Docker · GitHub Actions

| Deliverable from the requirements | Where |
|---|---|
| 1. High-level architecture / solution approach | [docs/02-architecture.md](docs/02-architecture.md) |
| 2. Data model | [docs/03-data-model.md](docs/03-data-model.md) + [migrations/001_init.sql](migrations/001_init.sql) |
| 3. API design | [docs/04-api.md](docs/04-api.md) + interactive docs at `/docs` + [docs/openapi.json](docs/openapi.json) |
| 4. Code implementation | [app/](app/), with 65 automated evals in [tests/](tests/) |
| Every requirement → design → code → test | [docs/01-requirements-traceability.md](docs/01-requirements-traceability.md) |
| Why each technology and design choice | [docs/05-decisions.md](docs/05-decisions.md) |
| Operating it in production | [docs/06-runbook.md](docs/06-runbook.md) |
| How to build a project like this yourself | [docs/07-build-it-yourself.md](docs/07-build-it-yourself.md) |

---

## What it guarantees

- **No duplicate payments.** Idempotency keys plus a unique payment reference, enforced by the database even under concurrent requests from many servers.
- **No double payment on retries.** The payment id is sent to the bank as its idempotency key, so a retry after a timeout returns the original transfer instead of moving money twice.
- **No lost payments.** The payment and its queue job are saved in one transaction; a crashed worker's payment is picked up again automatically.
- **No lost notifications.** Webhooks are written in the same transaction as the status change (transactional outbox), signed, retried, and delivered in order.
- **A complete, tamper-proof history.** Every status change is recorded with the reason, who made it, when, and the request id. The database refuses edits and deletes.
- **Rules that can't be bypassed.** Illegal status changes and changes to payment terms are rejected by the database itself, not just by the code.

## Quick start (Windows)

You need **Python 3.12** and **PostgreSQL 16**. (Or Docker: see below.)

**1. Create the databases** (in *SQL Shell (psql)* or pgAdmin, as the `postgres` user):

```sql
CREATE USER ach WITH PASSWORD 'ach' CREATEDB;
CREATE DATABASE ach OWNER ach;
CREATE DATABASE ach_test OWNER ach;
```

**2. Install** (PowerShell, in the project folder):

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

**3. Run the evals** (65 tests against the real system, about 1 minute):

```powershell
python -m pytest
```

**4. See it work:**

```powershell
python scripts/demo.py
```

The demo runs five scenarios (normal payment, flaky bank, closed account, bank down, bank timeout) plus duplicate submissions, and prints each payment's full audit trail.

**5. Run the service yourself** (two terminals):

```powershell
python -m app.main mock-bank      # terminal 1: the sandbox bank on port 4000
python -m app.main all            # terminal 2: migrate, then API + workers + webhooks on port 8000
```

Then create an API key and open the interactive API docs at **http://localhost:8000/docs** (click *Authorize* and paste the key):

```powershell
python -m app.main create-api-key --customer C12345 --name "Acme Corp"
```

Submit a payment from PowerShell:

```powershell
$key = "sk_..."   # the key printed above
$body = @{ customerId="C12345"; sourceAccount="VA10001"; destinationAccount="EXT98765"; amount=250.00; reference="PMT-1001" } | ConvertTo-Json
$p = Invoke-RestMethod -Method Post -Uri http://localhost:8000/v1/payments -ContentType "application/json" `
     -Headers @{ Authorization = "Bearer $key"; "Idempotency-Key" = "pmt-1001-key-01" } -Body $body
$p
(Invoke-RestMethod http://localhost:8000/v1/payments/$($p.id)/events -Headers @{ Authorization = "Bearer $key" }).events
```

### With Docker instead

```bash
docker compose up --build
```

This starts PostgreSQL, runs migrations, and starts the API, two workers, the webhook dispatcher, and the sandbox bank, each in its own container. *(The Docker and CI files follow standard patterns but were not executed in the environment this was built in; the Python path above is fully tested.)*

## How it works (one paragraph)

The API authenticates the caller, validates the request, checks for duplicates, and in **one database transaction** saves the payment as `PENDING`, writes its first audit event, and adds a job to a queue table. It replies `202 Accepted` immediately. Background **workers** (any number, on any machines) claim jobs with PostgreSQL's `FOR UPDATE SKIP LOCKED`, call the bank with the payment id as the idempotency key, and classify the result: accepted → `COMPLETED`; permanent rejection → `FAILED`; temporary problem → `RETRYING` with exponential backoff. A **circuit breaker** stops calling the bank during an outage. Every status change writes an audit event and webhook deliveries in the same transaction, and a **dispatcher** sends those webhooks, signed and in order. Details: [docs/02-architecture.md](docs/02-architecture.md).

## Testing ("evals")

```powershell
python -m pytest                     # 65 evals, real PostgreSQL, real HTTP, real background workers
python scripts/mutation_check.py     # injects 10 real bugs; every one must make the evals fail
```

| Suite | Proves |
|---|---|
| `test_01_submission` | The PDF's example request works; amounts are exact; 11 kinds of bad input are rejected with nothing stored; authentication, authorization, rate limiting |
| `test_02_duplicates_and_async` | Duplicates never create payments, including 30 identical requests at once and a deterministic race-condition regression test; processing is asynchronous, survives restarts, and competing workers never double-process |
| `test_03_lifecycle_and_resilience` | Legal transitions only (also enforced by the database); retries, permanent failures, giving up; **a bank timeout never pays twice**; **a crashed worker is recovered without paying twice**; circuit breaker |
| `test_04_status_audit_webhooks` | Status retrieval and pagination; a complete, append-only audit trail; signed, ordered, retried webhooks; SSRF protection; health checks and metrics |
| `test_05_chaos` | 80 payments × 3 concurrent submissions, a bank failing 30% at random, a webhook receiver failing 25%, 3 workers and 2 dispatchers competing. Then: no double payment, every payment final, every event delivered in order. |

**The evals found a real bug during development:** a race condition where a legitimate retry could get a wrong `409 Conflict`. It is fixed, documented in the code, and guarded by a deterministic regression test. The full story is in [docs/07-build-it-yourself.md](docs/07-build-it-yourself.md#a-real-bug-the-evals-found).

## Project layout

```
app/
  api.py          HTTP API: routes, request/response schemas, auth, rate limiting, errors
  payments.py     accepting payments, duplicate protection, the single status-change function
  worker.py       background processing: bank calls, retry decisions, crash recovery
  job_queue.py    durable PostgreSQL queue with leases (FOR UPDATE SKIP LOCKED)
  bank.py         bank client (error classification) and circuit breaker
  webhooks.py     endpoints, outbox, signatures, SSRF protection, dispatcher
  domain.py       business rules with no I/O: lifecycle, money, backoff, errors
  db.py           connection pool, transactions, migrations
  auth.py         API keys (stored hashed)
  metrics.py      Prometheus metrics
  logs.py         structured JSON logs with redaction
  config.py       validated settings
  platform.py     wires everything together
  main.py         command line: migrate | api | worker | dispatcher | all | mock-bank | create-api-key
migrations/       the database schema (SQL)
mock_bank/        sandbox banking partner with failure modes
tests/            the evals
scripts/          demo, mutation check, OpenAPI export
docs/             design documentation
```
