# Build log

This logs how AI was used to build the project.

## How it was built

I built this with **Claude (Anthropic) running as a coding agent**. I gave it the capstone brief, and it wrote the design doc, the code, the tests and the docs. It ran everything in a Linux sandbox against SQLite and a real PostgreSQL 16 instance. In plain terms: almost every line here was AI-generated. My job was to direct the work, review the output, and own understanding it.

What the AI couldn't do, and what's still on me:

- **Run real Stripe test-mode Checkout.** That needs my own Stripe account and `sk_test_` key, and the sandbox couldn't reach Stripe. Webhooks were tested with events signed the same way Stripe signs them, but not delivered by Stripe itself.
- **Run `docker compose up`.** There was no Docker daemon in the sandbox.

## Where AI helped most

- **Turning the brief into decisions.** Integer pico-USD for money, a per-tenant row lock to serialise billable requests, a jobs table as a transactional outbox, and storing only successful responses for idempotency (so a 402 doesn't "burn" the key).
- **Tests aimed at failure modes, not happy paths.** Exact quota boundaries, 20 concurrent retries of one key, forged and stale webhooks, out-of-order Stripe events, a job that fails three times.
- **Proving the tests catch bugs.** It temporarily removed the row lock and confirmed the concurrency test failed (`assert 14 == 1`, meaning 14 events were double-counted). Then it put the lock back.

## Where AI got it wrong, and what changed

These are real mistakes from this build, each caught by a failing test, a review pass or a live run:

| # | What was wrong | How it was caught | Fix |
|---|---|---|---|
| 1 | `migrations/env.py` resolved the database URL through a tangle of fallbacks. It wasn't obvious which URL a migration would hit. | Review | Rewrote with one precedence order: explicit `config.attributes["url"]` > `DATABASE_URL` > `alembic.ini`. |
| 2 | The background `Worker` class stored its stop flag in `self._stop`, which shadows a private method on Python's `threading.Thread`. That can break `join()`. | Review | Renamed to `_stop_event`. |
| 3 | Quota error said "needs 1 AI tokens" / "1 API calls". | Reading real responses | Pluralised labels by count. |
| 4 | Test helper used `body or SMALL`, so an empty body `{}` (falsy) was silently replaced by a valid default. The "empty body → 422" test got a 201. | Failing test | `SMALL if body is None else body`. |
| 5 | A job test parsed stored JSON with `eval()`. | Review | `json.loads`. |
| 6 | When one request crossed both 80% and 100%, the 80% alert read "You have used 80% … (100,000 of 100,000)", which contradicts itself. | Reading the live probe output | Now says "reached 80% … Current usage: 100,000 of 100,000." |
| 7 | Notification timestamps came back in the database server's local zone (`-05:00`), not UTC. | Reading the live probe output | Postgres connections set `timezone=UTC`, and the API normalises to UTC. A test now asserts `+00:00`. |
| 8 | A Postgres test run reported 51 failures. | Test run | Not a code bug: the suite was pointed at a database already seeded for the live demo. On a clean database: 67 passed. Lesson: the test database must be disposable. |

## Things I should be able to explain (and can)

- **Why a retry can't double-bill** (`app/metering.py`, `record_billable`). The tenant row is locked with `SELECT … FOR UPDATE`, so two requests for the same tenant run one at a time. Inside the lock, the idempotency key is looked up first. If it exists with the same body hash, the stored response is returned and nothing is written. The unique constraint on `usage_events` is a second line of defense.
- **Why the 1,001st call is rejected but the 1,000th isn't.** The rule is `used + requested <= limit`, checked before any insert. At 999 used, 999 + 1 = 1,000 ≤ 1,000 passes. At 1,000 used, 1,001 > 1,000 fails.
- **Why webhook dedupe and the plan change share one transaction** (`app/billing.py`, `handle_webhook`). If the event id were saved separately and the plan update then failed, Stripe's retry would be ignored as a "duplicate" and the customer would never get Pro. One commit means both happen or neither does.

## Time

Built over one working session, phase by phase: design, core billing, Stripe, pricing and finishing. Each phase has its own commit.
