# API design

Base path `/v1`. JSON everywhere. Interactive documentation (try every endpoint in the browser): **http://localhost:8000/docs**. Machine-readable spec: [openapi.json](openapi.json).

**Authentication:** `Authorization: Bearer sk_...` on every `/v1` request. Each key belongs to one customer and only sees that customer's data. **Request ids:** send `X-Request-Id` to trace a request across systems; every response returns one.

## Errors (same shape everywhere)

```json
{ "error": { "code": "VALIDATION_ERROR", "message": "The request is invalid.",
             "details": [{ "field": "amount", "message": "Must be greater than 0." }] } }
```

| Status | Code | When |
|---|---|---|
| 400 | `VALIDATION_ERROR`, `INVALID_JSON`, `IDEMPOTENCY_KEY_REQUIRED`, `INVALID_CURSOR` | Bad input (nothing is stored) |
| 401 | `UNAUTHORIZED` | Missing, invalid, or revoked API key |
| 403 | `CUSTOMER_MISMATCH` | `customerId` is not the authenticated customer |
| 404 | `PAYMENT_NOT_FOUND`, ... | Unknown id, or belongs to another customer |
| 409 | `DUPLICATE_REFERENCE`, `DELIVERY_NOT_RETRYABLE` | Reference already used (`details.existingPaymentId`) |
| 422 | `IDEMPOTENCY_KEY_REUSED` | Same key, different request body |
| 429 | `RATE_LIMITED` | Too many requests for this key |

## `POST /v1/payments`: submit a payment

Headers: **`Idempotency-Key`** (required; 8–255 characters; unique per payment, reused only to retry the same request).

```json
{ "customerId": "C12345", "sourceAccount": "VA10001", "destinationAccount": "EXT98765", "amount": 250.00, "reference": "PMT-1001" }
```

`amount` may be a number or a string, with at most 2 decimals, maximum $1,000,000. Unknown fields are rejected (typo protection).

- **`202 Accepted`**: created and queued. `Location: /v1/payments/{id}`.
- **`200 OK`** with `Idempotent-Replayed: true`: a retry of an earlier request; the original payment is returned and nothing new is created.

```json
{
  "id": "pay_65eabfa0d5894e1bb6d1e50b53bc32cf",
  "customerId": "C12345",
  "sourceAccount": "****0001",
  "destinationAccount": "****8765",
  "amount": "250.00",
  "amountCents": 25000,
  "currency": "USD",
  "reference": "PMT-1001",
  "status": "PENDING",
  "attempts": 0,
  "bankTransferId": null,
  "lastError": null,
  "createdAt": "2026-10-05T03:57:03.733561+00:00",
  "updatedAt": "2026-10-05T03:57:03.733561+00:00",
  "completedAt": null
}
```

Account numbers are always masked in responses, webhooks, and logs.

## `GET /v1/payments/{id}`: status

Returns the payment object above. `lastError` is `{ "code", "message" }` after a failed attempt.

## `GET /v1/payments/{id}/events`: audit trail

```json
{ "paymentId": "pay_...", "events": [
  { "id": "evt_1", "sequence": 1, "from": null, "to": "PENDING", "reason": "Payment accepted and queued for processing",
    "actor": "api", "requestId": "req_...", "metadata": {}, "createdAt": "..." },
  { "id": "evt_2", "sequence": 2, "from": "PENDING", "to": "PROCESSING", "reason": "Attempt 1 of 6: submitting transfer to bank",
    "actor": "worker:host:1234:ab12cd#0", "requestId": "req_...", "metadata": { "attempt": 1 }, "createdAt": "..." }
]}
```

## `GET /v1/payments`: list

Query: `status`, `reference`, `limit` (1–100, default 20), `cursor`. Newest first: `{ "data": [...], "nextCursor": "..." | null }`. Pass `nextCursor` back as `cursor` for the next page.

## Webhooks

| Endpoint | Purpose |
|---|---|
| `POST /v1/webhook-endpoints` `{ "url", "description"? }` | Register. Returns `201` with the signing `secret` (**shown only once**) |
| `GET /v1/webhook-endpoints` | List (without secrets) |
| `DELETE /v1/webhook-endpoints/{id}` | Disable; undelivered events are cancelled |
| `POST /v1/webhook-deliveries/{id}/retry` | Re-send a delivery that gave up (`202`) |

**What you receive:** one `POST` per status change, with event types `payment.pending`, `payment.processing`, `payment.retrying`, `payment.completed`, `payment.failed`.

```json
{ "id": "evt_3", "type": "payment.completed", "sequence": 3, "createdAt": "...",
  "data": { "previousStatus": "PROCESSING", "reason": "Bank accepted transfer trf_...", "payment": { "...": "payment object" } } }
```

Headers: `ACH-Event-Id`, `ACH-Delivery-Attempt`, and `ACH-Signature: t=<unix time>,v1=<hex>`.

**Delivery rules:** retried with exponential backoff on non-2xx or timeout; **in order per payment**; **at least once**, so de-duplicate on `ACH-Event-Id`.

**Verifying a webhook** (receiver side, Python):

```python
import hashlib, hmac, time

def verify(secret: str, raw_body: str, header: str, tolerance_s: int = 300) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(","))
    t, received = int(parts["t"]), parts["v1"]
    if abs(time.time() - t) > tolerance_s:          # reject replays of old messages
        return False
    expected = hmac.new(secret.encode(), f"{t}.{raw_body}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)  # constant-time comparison
```

## Operations (not under `/v1`, no API key)

`GET /health/live` (process up), `GET /health/ready` (database reachable, else `503`), `GET /metrics` (Prometheus; expose only on the internal network).
