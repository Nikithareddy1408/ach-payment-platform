-- ACH payment platform: initial schema.
-- Rules that protect money are enforced HERE, in the database, not only in
-- application code: uniqueness (no duplicate payments), legal status
-- transitions, immutable payment terms, and an append-only audit log.

-- ─── Customers and API keys ─────────────────────────────────────────────
CREATE TABLE customers (
  id          text PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9_-]{1,64}$'),
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
  id          text PRIMARY KEY,
  customer_id text NOT NULL REFERENCES customers(id),
  key_hash    text NOT NULL UNIQUE,          -- SHA-256 of the key; the key itself is never stored
  key_prefix  text NOT NULL,                 -- first characters, to identify a key in logs/UI
  created_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz
);
CREATE INDEX api_keys_customer_idx ON api_keys (customer_id);

-- ─── Payments ───────────────────────────────────────────────────────────
CREATE TABLE payments (
  id                  text PRIMARY KEY,
  customer_id         text NOT NULL REFERENCES customers(id),
  source_account      text NOT NULL,
  destination_account text NOT NULL,
  amount_cents        bigint NOT NULL CHECK (amount_cents > 0),
  currency            char(3) NOT NULL DEFAULT 'USD',
  reference           text NOT NULL,
  status              text NOT NULL CHECK (status IN ('PENDING','PROCESSING','COMPLETED','FAILED','RETRYING')),
  attempts            integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  bank_transfer_id    text,
  last_error_code     text,
  last_error_message  text,
  idempotency_key     text NOT NULL,
  request_hash        text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  completed_at        timestamptz,
  CONSTRAINT payments_source_ne_destination CHECK (source_account <> destination_account),
  -- Duplicate protection, enforced even under concurrent requests from many servers:
  CONSTRAINT payments_idempotency_key_uq UNIQUE (customer_id, idempotency_key),
  CONSTRAINT payments_reference_uq       UNIQUE (customer_id, reference)
);
CREATE INDEX payments_customer_created_idx ON payments (customer_id, created_at DESC);
CREATE INDEX payments_open_idx ON payments (status, updated_at) WHERE status IN ('PENDING','PROCESSING','RETRYING');

-- Defense in depth: the database itself refuses illegal status changes and
-- any change to what was agreed (who pays whom, how much).
CREATE FUNCTION payments_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.customer_id IS DISTINCT FROM OLD.customer_id
     OR NEW.source_account IS DISTINCT FROM OLD.source_account
     OR NEW.destination_account IS DISTINCT FROM OLD.destination_account
     OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
     OR NEW.currency IS DISTINCT FROM OLD.currency
     OR NEW.reference IS DISTINCT FROM OLD.reference
     OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
    RAISE EXCEPTION 'payment terms are immutable (payment %)', OLD.id USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
       (OLD.status, NEW.status) IN (
         ('PENDING',    'PROCESSING'),
         ('PROCESSING', 'COMPLETED'),
         ('PROCESSING', 'FAILED'),
         ('PROCESSING', 'RETRYING'),
         ('RETRYING',   'PROCESSING'))) THEN
    RAISE EXCEPTION 'illegal payment status transition % -> % (payment %)', OLD.status, NEW.status, OLD.id
      USING ERRCODE = 'check_violation';
  END IF;

  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE TRIGGER payments_guard BEFORE UPDATE ON payments FOR EACH ROW EXECUTE FUNCTION payments_guard();

-- ─── Audit trail (append-only) ──────────────────────────────────────────
CREATE TABLE payment_events (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  payment_id  text NOT NULL REFERENCES payments(id),
  from_status text,
  to_status   text NOT NULL,
  reason      text NOT NULL,
  actor       text NOT NULL,                 -- 'api' | 'worker:<id>' | 'system'
  request_id  text,                          -- correlates with API/worker logs
  metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX payment_events_payment_idx ON payment_events (payment_id, id);

CREATE FUNCTION payment_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'payment_events is append-only' USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER payment_events_no_update BEFORE UPDATE ON payment_events
  FOR EACH ROW EXECUTE FUNCTION payment_events_append_only();
CREATE TRIGGER payment_events_no_delete BEFORE DELETE ON payment_events
  FOR EACH ROW EXECUTE FUNCTION payment_events_append_only();

-- ─── Durable work queue (claimed with FOR UPDATE SKIP LOCKED) ───────────
CREATE TABLE payment_jobs (
  payment_id       text PRIMARY KEY REFERENCES payments(id),
  state            text NOT NULL CHECK (state IN ('QUEUED','RUNNING','DONE')),
  run_at           timestamptz NOT NULL DEFAULT now(),
  lease_token      uuid,
  locked_by        text,
  lease_expires_at timestamptz,
  claims           integer NOT NULL DEFAULT 0,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX payment_jobs_ready_idx ON payment_jobs (run_at) WHERE state = 'QUEUED';
CREATE INDEX payment_jobs_lease_idx ON payment_jobs (lease_expires_at) WHERE state = 'RUNNING';

-- ─── Webhooks (transactional outbox) ────────────────────────────────────
CREATE TABLE webhook_endpoints (
  id          text PRIMARY KEY,
  customer_id text NOT NULL REFERENCES customers(id),
  url         text NOT NULL,
  secret      text NOT NULL,
  description text,
  enabled     boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now(),
  disabled_at timestamptz
);
CREATE INDEX webhook_endpoints_customer_idx ON webhook_endpoints (customer_id) WHERE enabled;

CREATE TABLE webhook_deliveries (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  endpoint_id      text NOT NULL REFERENCES webhook_endpoints(id),
  payment_id       text NOT NULL REFERENCES payments(id),
  event_id         bigint NOT NULL REFERENCES payment_events(id),
  payload          text NOT NULL,             -- exact bytes that get signed and sent
  state            text NOT NULL CHECK (state IN ('PENDING','DELIVERING','DELIVERED','FAILED')),
  attempts         integer NOT NULL DEFAULT 0,
  next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  lease_expires_at timestamptz,
  last_error       text,
  last_status_code integer,
  delivered_at     timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT webhook_deliveries_endpoint_event_uq UNIQUE (endpoint_id, event_id)
);
CREATE INDEX webhook_deliveries_due_idx ON webhook_deliveries (next_attempt_at) WHERE state = 'PENDING';
CREATE INDEX webhook_deliveries_order_idx ON webhook_deliveries (endpoint_id, payment_id, id);
CREATE INDEX webhook_deliveries_stuck_idx ON webhook_deliveries (lease_expires_at) WHERE state = 'DELIVERING';
