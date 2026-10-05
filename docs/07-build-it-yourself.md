# How to build a project like this yourself

This is the process used to build this project, written so you can repeat it on any backend task without help.

## Step 1: Turn the requirements into a numbered checklist

Read the document twice. Underline every **noun** (payment, account, webhook, status) and every **verb** (submit, process, retry, retrieve, notify). Each verb is usually a requirement. Give each an id:

> R1 submit payment · R2 lifecycle states · R3 async · R4 queue · R5 bank integration · R6 retries · R7 no duplicates · R8 status API · R9 webhooks · R10 audit trail

Also read the *business context* paragraph carefully. "Banking systems are not always available and network failures occur regularly" is not decoration: it is the hardest requirement in the document.

## Step 2: Write down your assumptions

Real tasks are ambiguous. Decide, and write it down:

- Who calls the API? *Enterprise systems with API keys → authentication needed.*
- What does COMPLETED mean? *The bank accepted the transfer (real ACH settlement comes later: out of scope, noted in the runbook).*
- How much money? *USD, max $1,000,000, cents precision.*
- Can a client retry? *Yes, always, so every operation must be safe to repeat.*

## Step 3: Ask "what can go wrong?" for every requirement

This step produces most of the real design. Make a table:

| Requirement | What can go wrong? | Design answer |
|---|---|---|
| R1 submit | Bad input, huge amounts, typos in field names | Strict validation, cents, reject unknown fields |
| R7 duplicates | Client retries after a timeout; two requests arrive at the same instant | Idempotency key + database unique constraints |
| R5 bank | Bank down; bank slow; **bank times out after moving the money** | Classify errors; retry with backoff; same idempotency key every time |
| R4 queue | Server restarts; worker crashes mid-payment; two workers take the same job | Queue in the database; leases; `SKIP LOCKED` |
| R9 webhooks | Crash after status change but before sending; customer's server down | Outbox in the same transaction; retries |

If you can't fill in the last column, research that problem before writing code. (Search terms that unlock this whole project: *idempotency key*, *transactional outbox*, *SKIP LOCKED queue*, *exponential backoff with jitter*, *circuit breaker*.)

## Step 4: Design on paper, in this order

1. **State diagram** of the main object (the payment lifecycle). Everything else hangs off it.
2. **Data model:** one table per noun; add constraints for every rule that protects money.
3. **API contract:** endpoints, example requests and responses, every error code.
4. **Architecture:** which processes exist and how data flows between them.
5. **Decisions:** for each technology, write the alternatives and why you chose one ([05-decisions.md](05-decisions.md)).

## Step 5: Build in thin slices, each with its tests

Don't write all the code and then test. Build one requirement at a time, and don't move on until its tests pass. The order used here:

1. Project skeleton: settings, database connection, first migration (`config.py`, `db.py`, `migrations/`)
2. Pure business rules with unit tests: lifecycle, money, backoff (`domain.py`)
3. Submit + get payment, with validation tests (`api.py`, `payments.py`, `test_01`)
4. Duplicate protection, with concurrency tests (`test_02`)
5. Queue + worker + sandbox bank, happy path first (`job_queue.py`, `worker.py`, `mock_bank/`)
6. Failure handling one case at a time: flaky bank → permanent error → bank down → timeout → crash (`test_03`)
7. Audit trail (`test_04`)
8. Webhooks: outbox → dispatcher → signatures → retries → SSRF protection (`webhooks.py`, `test_04`)
9. Production concerns: auth, rate limits, metrics, logs, health checks
10. Chaos test (`test_05`), then mutation testing (`scripts/mutation_check.py`)
11. Documentation and demo

## Step 6: Attack your own system

- **Integration tests against a real database.** Mocks hide exactly the bugs that matter here: transactions, locks, and races.
- **Concurrency tests:** fire 30 identical requests at once and count the results.
- **Chaos tests:** random bank failures + random webhook failures + competing workers, then check the invariants (no double payment, nothing lost, history intact).
- **Run the suite many times.** A test that fails 1 time in 10 is pointing at a real race condition, or it is a bad test. Either way, find out which.
- **Mutation testing:** break the code on purpose and confirm a test fails. A test that passes on broken code proves nothing.

## A real bug the evals found

During development, one full run out of ten failed: 30 identical simultaneous requests returned 29 × `200` as expected, except that 2 came back `409 Conflict`. Investigating (rather than re-running until green) showed a genuine race condition:

```
Request A: inserts the payment (not committed yet)
Request B: "any payment with this idempotency key?"  -> no   (A not committed yet)
Request A: COMMITS
Request B: "any payment with this reference?"        -> YES  (A is committed now)
Request B: returns 409 DUPLICATE_REFERENCE            <- wrong: it was a legitimate retry
```

In PostgreSQL's default isolation level, **each statement** sees the latest committed data, so two separate queries can straddle another transaction's commit. No money was at risk (the database constraints held), but a client could wrongly conclude its payment failed. **The fix:** one query that checks both conditions, so both see the same snapshot (`payments.py`, `_create_in_tx`).

Two more lessons came out of it:

1. The first regression test fired random bursts and **passed even with the bug put back**, because it only hit the race window sometimes. It was replaced by a test that forces the exact interleaving every time (`test_regression_duplicate_committing_mid_check...` in `test_02`).
2. The first version of the *mutation* for this bug didn't faithfully reproduce the original code, so it was "missed". Checking the checker matters too.

## Python concepts used, and where to see them

| Concept | Where to look | What to learn |
|---|---|---|
| Type hints, `dataclass` | `payments.py` (`Actor`, `NewPayment`) | Describe data shapes clearly |
| Context managers (`with`) | `db.py` `transaction()` | Automatic commit/rollback and cleanup |
| Decorators | `api.py` (`@v1.post(...)`) | How FastAPI turns functions into endpoints |
| Pydantic models | `api.py` `CreatePaymentRequest`, `config.py` | Validation from type declarations |
| Dependency injection | `api.py` `Depends(current_customer)` | Reusable auth for every route |
| Threads + `Event` | `loop.py` | Background workers and graceful shutdown |
| Exceptions as control flow | `domain.py` `ApiError` | One place turns errors into HTTP responses |
| SQL transactions, `FOR UPDATE`, `SKIP LOCKED` | `job_queue.py`, `payments.py` | The core of correct concurrent systems |
| pytest fixtures, `parametrize` | `tests/conftest.py`, `test_01` | Setup/teardown and table-driven tests |

## Explaining it in 60 seconds

> "Clients submit payments through an authenticated API. The API saves the payment and a queue job in one transaction and answers immediately. Workers pick up jobs and call the bank, always using the payment id as the bank's idempotency key, so even if the bank times out after moving the money, a retry can never pay twice. Temporary errors retry with backoff, permanent ones fail fast, and a circuit breaker protects the bank during outages. Every status change is written to an append-only audit trail and a webhook outbox in the same transaction, so nothing is lost. The database itself enforces the critical rules. 65 automated tests, including a chaos test, prove it; mutation testing proves the tests catch real bugs; and the tests found a real race condition that I fixed."

## Checklist before you call any project done

- [ ] Every requirement has an id, a design answer, code, and a test ([01-requirements-traceability.md](01-requirements-traceability.md))
- [ ] Every "what can go wrong?" has an answer and a test
- [ ] The full test suite passes many times in a row
- [ ] Deliberately broken code makes the tests fail
- [ ] Someone else can run it from the README alone
- [ ] Decisions and trade-offs are written down, including what you chose **not** to build
