Playbook: Add a requirement to the ACH payment platform, with evals

## Overview
Implement one new requirement and prove it works with automated tests.

## Required from User
- The requirement, in one or two sentences.
- The response for success and for each error case (status code and body).

## Procedure
1. Read AGENTS.md, README.md and docs/01-requirements-traceability.md.
2. Write down, in the session, which files you expect to change and why.
3. Write new tests in tests/ that describe the requirement. Run them and confirm they fail.
4. Implement the change in app/ (and a new migration file if the database changes).
5. Run the full suite with @skills:run-evals. All tests must pass.
6. Add one entry to scripts/mutation_check.py that breaks the new behavior,
   and confirm the new tests catch it by running only that mutation.
7. Add a row to docs/01-requirements-traceability.md.
8. Open a pull request with: what changed, the new tests, and the eval results.

## Specifications
- All tests pass, including the new ones.
- The new mutation is reported as caught.
- The traceability document has a row for the requirement.

## Advice
- Follow every rule in AGENTS.md.
- Keep changes small. Do not refactor unrelated code.
- If a new migration replaces a database function, check scripts/mutation_check.py
  for mutations that target the old migration file, and point them at the new one.

## Forbidden Actions
- Do not delete or weaken existing tests.
- Do not edit existing migration files.
- Do not change the bank idempotency key.
