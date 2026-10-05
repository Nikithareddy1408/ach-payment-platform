# Design decisions

Each decision records the options considered and why one was chosen (a lightweight version of "Architecture Decision Records", common at engineering companies).

## 1. Language and framework: Python + FastAPI

| Option | Strengths | Why not chosen here |
|---|---|---|
| **Python + FastAPI** ✅ | Readable; type hints + Pydantic validate every request; automatic OpenAPI docs; huge ecosystem; widely used for fintech APIs | |
| Java + Spring Boot | The most common choice at banks; very mature transactions and tooling | Equally valid. Not used because the author works in Python, and the design transfers 1:1 |
| Go | Fast, simple concurrency, popular at fintech startups | New language for the author |
| Node.js / TypeScript | Good for I/O-heavy APIs | New language for the author |

The architecture (outbox, idempotency, leases, circuit breaker) is language-independent. That is the part that matters.

## 2. Database: PostgreSQL

Payments need real ACID transactions, unique constraints, row locking, and triggers. PostgreSQL provides all of them and is the standard choice for financial systems. A NoSQL store would push consistency rules into application code, where races are easy to get wrong.

## 3. Queue: PostgreSQL table with `FOR UPDATE SKIP LOCKED`

| Option | Trade-off |
|---|---|
| **PostgreSQL queue** ✅ | Job is written **in the same transaction** as the payment, so they can never disagree. No extra infrastructure. Handles thousands of jobs per second |
| Kafka / RabbitMQ / SQS | Huge scale and fan-out, but writing to the database **and** the broker is a "dual write": a crash between them loses or duplicates work, unless you add an outbox anyway |
| Celery + Redis | Popular in Python, but Redis can lose queued jobs on a crash unless carefully configured |

**When to add Kafka/SQS:** when other internal services need to consume payment events at high volume. The outbox table becomes the source that feeds the broker, so the guarantees stay the same.

## 4. Duplicate protection: two layers, enforced by the database

`Idempotency-Key` handles client retries (network blips, timeouts); unique `(customer, reference)` catches the same business payment under a different key. Both are **database constraints**, because application-level checks alone can race. The lookup is a single statement so it sees one consistent snapshot. (A two-statement version was a real bug, found by the evals.)

## 5. Bank idempotency: the payment id is the bank's idempotency key

A timeout is ambiguous: the bank may or may not have moved the money. Reusing the same key on every attempt lets the bank return the original result, which is the only way to retry safely.

## 6. Webhooks: transactional outbox, at-least-once, signed

Writing the delivery row in the status-change transaction guarantees nothing is lost. Exactly-once delivery over a network is impossible (a lost acknowledgement is indistinguishable from a failure), so delivery is at-least-once with event ids for de-duplication: the same model Stripe, GitHub, and Shopify use. HMAC signatures with a signed timestamp prove authenticity and block replays.

## 7. Money as integer cents

`0.1 + 0.2 != 0.3` in floating point. Integer cents (and `Decimal` while parsing) keep every amount exact.

## 8. Circuit breaker around the bank

Without it, an outage makes every worker hammer a failing bank, and every payment burns its retries during the outage. The breaker defers work without spending attempts and probes with a single trial request. It is per process; with many instances, a shared breaker (e.g. in Redis) would react faster.

## 9. Synchronous code with threads (not asyncio)

Workers are I/O-bound and few; threads are simple, debuggable, and well supported by psycopg and httpx. FastAPI runs synchronous endpoints in a thread pool. `asyncio` would help with tens of thousands of concurrent connections per process; this service scales out by adding instances instead.

## 10. Rules enforced in the database, not only in code

Legal transitions, immutable payment terms, uniqueness, and the append-only audit log are enforced by constraints and triggers. Bugs, new code paths, and manual SQL fixes cannot violate them. The tests try exactly that.
