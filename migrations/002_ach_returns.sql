-- ACH returns: a COMPLETED payment can later be RETURNED by the receiving bank
-- (return codes R01-R99). The only new legal status change is COMPLETED -> RETURNED.

ALTER TABLE payments DROP CONSTRAINT payments_status_check;
ALTER TABLE payments ADD CONSTRAINT payments_status_check
  CHECK (status IN ('PENDING','PROCESSING','COMPLETED','FAILED','RETRYING','RETURNED'));

CREATE OR REPLACE FUNCTION payments_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
         ('RETRYING',   'PROCESSING'),
         ('COMPLETED',  'RETURNED'))) THEN
    RAISE EXCEPTION 'illegal payment status transition % -> % (payment %)', OLD.status, NEW.status, OLD.id
      USING ERRCODE = 'check_violation';
  END IF;

  NEW.updated_at := now();
  RETURN NEW;
END $$;
