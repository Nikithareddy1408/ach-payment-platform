# Instructions for AI agents

## What this project is
An ACH payment processing service: FastAPI API, PostgreSQL, background
workers, sandbox bank. See README.md and docs/02-architecture.md.

## Setup
1. Python 3.12+ and PostgreSQL must be running.
2. As the postgres superuser, create the user and databases:
   CREATE USER ach WITH PASSWORD 'ach' CREATEDB;
   CREATE DATABASE ach OWNER ach;
   CREATE DATABASE ach_test OWNER ach;
3. pip install -r requirements.txt
4. cp .env.example .env

## Rules you must never break
- Money is always integer cents (amount_cents). Never use floats for money.
- Every payment status change goes through PaymentService.transition() in app/payments.py.
- The bank idempotency key is always the payment id. Never change it.
- Allowed status changes are defined in app/domain.py AND in the payments_guard
  trigger in migrations/. Change both together, in a NEW migration file.
- Never edit an existing migration file. Add a new one (002_..., 003_...).
- Never delete or weaken existing tests to make the suite pass.
- Never log or return full account numbers.

## Definition of done
- `python -m pytest` passes (all tests).
- New behavior has new tests.
- docs/01-requirements-traceability.md is updated for new requirements.