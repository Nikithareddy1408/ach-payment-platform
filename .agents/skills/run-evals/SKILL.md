---
name: run-evals
description: Run the ACH test suite and report the results. Use before opening any pull request.
---

## Setup
1. Make sure PostgreSQL is running and the ach_test database exists (see AGENTS.md).
2. Install dependencies: `pip install -r requirements.txt`

## Run
1. Run `python -m pytest` and record how many tests passed and failed.
2. Only if the user asks for the mutation check, run `python scripts/mutation_check.py`
   and record how many injected bugs were caught.

## Report
1. Report: tests passed / total (and mutations caught / total, if run).
2. If any test failed, show the test name and the assertion message.
3. Do not change any code while running this skill.