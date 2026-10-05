# Operations runbook

## What to alert on

| Signal (Prometheus) | Alert when | Likely cause → first action |
|---|---|---|
| `ach_oldest_open_payment_age_seconds` | > 15 minutes and rising | Workers down or bank outage → check worker logs and `ach_bank_circuit_state` |
| `ach_payment_jobs{state="QUEUED"}` | growing for 10 minutes | Workers can't keep up → scale workers (`docker compose up --scale worker=6`) |
| `ach_bank_circuit_state` | `2` (open) for > 5 minutes | Bank outage → contact the banking partner; payments will resume automatically |
| `ach_payment_transitions_total{to="FAILED"}` rate | spike | Read `last_error_code` on recent failures: bank rejections vs. retries exhausted |
| `ach_bank_request_duration_seconds` p95 | > 2s | Bank degradation; consider raising `BANK_TIMEOUT_MS` (keep `JOB_LEASE_MS` > 2×) |
| `ach_webhook_delivery_attempts_total{result="gave_up"}` | > 0 | A customer's endpoint is down → tell the customer; then use the retry endpoint |
| `/health/ready` | non-200 | Database unreachable |

## Common procedures

**Investigate a payment.** `GET /v1/payments/{id}/events` shows every step with the reason and the `requestId`; search the logs for that request id to see the matching log lines.

**Payment stuck in PROCESSING.** Normally impossible to stay stuck: when the worker's lease expires, another worker recovers it. Check `payment_jobs.lease_expires_at` for the payment, and confirm workers are running.

**Bank outage.** Nothing to do: the circuit breaker opens, payments wait without spending attempts, and processing resumes automatically after the outage. Payments that exhausted their attempts are `FAILED` with `last_error_code`; resubmit them under a new reference after confirming with the bank that no transfer exists for the old payment id.

**Webhooks failing for a customer.** Deliveries retry with backoff, then become `FAILED`. After the customer fixes their endpoint: `POST /v1/webhook-deliveries/{id}/retry`.

**Rotate a customer's API key.** `python -m app.main create-api-key --customer <id>`, give the customer the new key, then revoke the old one (`revoke_api_key`, or set `api_keys.revoked_at`).

**Deploy.** Run `python -m app.main migrate` first (safe to run repeatedly and concurrently), then roll out api / worker / dispatcher. On SIGTERM, workers finish their current payment before exiting. Even a hard kill is safe: leases expire and another worker recovers the payment.

## Production roadmap (deliberately out of scope)

- **ACH realities:** settlement takes 1–2 banking days and payments can be **returned** later (e.g. R01 insufficient funds, R10 unauthorized), so a production lifecycle adds `SETTLED` and `RETURNED`. Also: NACHA file generation, cut-off times, banking holidays, same-day ACH limits.
- **Secrets:** encrypt account numbers and webhook secrets at rest with a key management service (AWS KMS / Vault); support secret rotation.
- **Scale:** shared rate limiting and circuit-breaker state (Redis or API gateway); partition `payment_events` by month; read replicas for reporting.
- **Observability:** distributed tracing (OpenTelemetry) across API → worker → bank.
- **Data retention** policy for audit logs and webhook payloads; backups with point-in-time recovery.
