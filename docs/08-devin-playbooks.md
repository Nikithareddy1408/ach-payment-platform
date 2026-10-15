# Working with Devin: AGENTS.md, skills, and playbooks

This document records how I set up Devin (Cognition's AI software engineer) to work on this project, what happened in each session, and how I improved the instructions after each run.

**The goal:** not just to use an AI agent once, but to give it reusable instructions, so it completes a whole category of work reliably, and to improve those instructions with every session.

---

## 1. What I built

| Piece | File | Purpose |
|---|---|---|
| **Project rules** | [`AGENTS.md`](../AGENTS.md) | Read by any AI agent at the start of every session: how to set up the project, rules that must never be broken (money in integer cents, the bank idempotency key is always the payment id, never edit an existing migration, never weaken tests), and what "done" means |
| **Skill: `run-evals`** | [`.agents/skills/run-evals/SKILL.md`](../.agents/skills/run-evals/SKILL.md) | A repeatable procedure: set up, run the test suite, report results. The expensive mutation check only runs when asked |
| **Playbook: `!ach-requirement`** | [`playbooks/ach-requirement.devin.md`](../playbooks/ach-requirement.devin.md) (copy of the version in Devin) | A reusable recipe for adding any requirement: plan the files, write failing tests first, implement, run the evals, add a mutation that proves the tests catch a regression, update the traceability document, open a pull request |

**How they fit together:** `AGENTS.md` sets the rules for every task. The skill is a procedure tied to this repository. The playbook is the recipe for one type of task, and it calls the skill (`@skills:run-evals`) to verify its own work.

---

## 2. The three sessions

### Session 1: run the evals

**Task:** "Set up this repository following AGENTS.md, then run the evals `@skills:run-evals`. Do not change any code."

**Result:** 65/65 tests passed. No code changed.

**What Devin ran into, and solved by itself:**
- The machine had Python 3.10; the project needs 3.12. Devin created a Python 3.12 virtual environment.
- It installed PostgreSQL 14. All 65 tests failed at first with `must be owner of schema public`: in PostgreSQL 14 the `public` schema belongs to the `postgres` superuser, but the tests drop and recreate it. Devin fixed the ownership on the database side instead of changing the tests.

**Lesson → improvement:** both gaps were in my setup instructions. I added a Python 3.12 fallback and the PostgreSQL 14 schema-ownership step to `AGENTS.md`, so no future agent has to rediscover them. Devin also saved its environment setup for future sessions.

### Session 2: ACH returns (first use of the playbook)

**Requirement:** a `RETURNED` status (only `COMPLETED → RETURNED` allowed), an endpoint `POST /v1/payments/{id}/returns` with return codes R01–R99, the return code recorded in the audit trail, and a `payment.returned` webhook.

**Result:** [PR #1](https://github.com/Nikithareddy1408/ach-payment-platform/pull/1). 22 new tests (87 total). CI passed with all 11 mutations. I ran the tests myself (87 passed), then merged.

**What went well:**
- Posted its plan before coding, as the playbook requires.
- Created a **new** migration (`002_ach_returns.sql`) instead of editing `001`, and updated the status rules in both the code and the database trigger.
- Noticed on its own that a return would overwrite the original completion time, and prevented it.
- Noticed that the new migration silently disabled one existing mutation (it targeted the old migration file) and fixed it.

**What showed gaps in my instructions:**
- An existing test had to change, because the requirement changes the set of allowed status changes. My rule ("never delete or weaken tests") didn't cover legitimate changes. Devin flagged it and explained it, which was the right behavior, but my rule didn't tell it to.
- My requirement didn't say what a successful return should respond with, so Devin chose `200` on its own.

**Lessons → improvements:**
- `AGENTS.md`: if a requirement changes intended behavior and a test must change, say so **before** changing it, explain why, and keep the test at least as strong.
- Playbook, *Required from User*: the requirement must state the response for success and every error case.
- Playbook, *Advice*: when a new migration replaces a database function, update mutations that target the old migration.

### Session 3: daily payment limit (improved playbook)

**Requirement:** at most $50,000 per customer per UTC day (configurable as `DAILY_LIMIT_CENTS`), safe under concurrent requests, `422 DAILY_LIMIT_EXCEEDED` with details when over the limit, and same-key retries still return the original payment. Every response was specified in the requirement.

**Result:** [PR #2](https://github.com/Nikithareddy1408/ach-payment-platform/pull/2). 16 new tests (103 total). CI passed with all 12 mutations. I ran the tests myself (103 passed), then merged.

**What went well:**
- **No guessing:** every response was specified, so Devin made no decisions on its own about status codes or error bodies.
- **Followed the new test rule:** it flagged, *before acting*, that an existing $1,000,000 test would conflict with the new limit, and found a way to avoid changing any test (it raised the limit only in the test harness, the same way the harness already relaxes the rate limit; the new tests use the real $50,000 limit).
- **Thought through the hard case:** it takes the per-customer lock *before* the idempotency check, so a same-key retry arriving at the same moment as the original still gets `200`, not `422`.
- **Tested concurrency two ways:** 12 simultaneous $10,000 payments give exactly 5 accepted, and a second test forces two requests to overlap; it fails every time the lock is removed. Its new mutation ("advisory lock removed") is caught.

---

## 3. Before and after

| | Session 2 (first use) | Session 3 (improved instructions) |
|---|---|---|
| Decisions Devin had to make on its own | Chose the success status code | None |
| Existing tests | Changed one (flagged after planning) | Changed none (flagged *before* acting, found an alternative) |
| Harder case handled | Kept the original completion time | Concurrency lock ordering, proven with a deterministic test |
| My review | Ran 87 tests locally, merged | Ran 103 tests locally, merged |

---

## 4. Cost

| Session | Cost |
|---|---|
| 1. Run the evals | $0.64 |
| 2. ACH returns | $2.41 |
| 3. Daily limit | $2.19 |

Total: $5.24 of the $10 starting credit.

---

## 5. What I learned

1. **The quality of the agent's work depends on the quality of the instructions.** Every session exposed a gap, and each fix made the next session better.
2. **Write requirements so nothing is left to guess.** Anything not specified is decided by the agent.
3. **Rules need to cover legitimate exceptions.** "Never change tests" was too strict; "flag it first, explain, keep it as strong" works.
4. **Always verify; never just trust the report.** I ran every PR's tests on my own machine before merging, and CI ran the full suite plus the mutation check on every PR.
5. **One task, one session, one pull request.** Fresh sessions read the latest instructions and give a fair test of them.

**Division of work:** Devin wrote the feature code and tests. I designed the instructions (`AGENTS.md`, skill, playbook), wrote the requirements, reviewed and verified every PR, and improved the instructions after each session.

## 6. Next steps

- A second playbook for a **security review** of payment code (account numbers in logs, SQL built from strings, missing authentication, webhook SSRF checks).
- An **automation** that runs the `run-evals` skill on every new pull request.
- Reusing this setup (rules, skill, playbook pattern) on my other projects.
