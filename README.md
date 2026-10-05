# Usage Metering & Billing Engine

A multi-tenant backend that meters LLM usage, enforces monthly quotas, prices tokens exactly, and upgrades tenants from Free to Pro through Stripe Checkout (test mode). FlyRank Backend capstone.

It answers four questions on every billable request:

1. **Did this happen already?** A retried request (same `Idempotency-Key`) is recorded once and gets the original response back.
2. **Is the tenant allowed?** Quota is checked *before* anything is written. Over quota is a 402 (Free: upgrade) or 429 (Pro: wait for reset), with a message a human can act on.
3. **What did it cost?** Fresh input, cached input, output and reasoning tokens each have a pinned price. Money is stored in integer pico-USD so nothing drifts.
4. **What plan are they on?** Only a verified, deduplicated Stripe webhook can change it.

The model call itself is simulated. This project meters numbers; it doesn't call an LLM.

## Architecture

```
                 ┌──────────────── HTTP layer (app/main.py) ────────────────┐
 client ──────▶  │ auth (X-API-Key) · boundary validation (pydantic, 422)  │
 X-API-Key       │ Idempotency-Key header · one error shape · no SQL here   │
                 └───────────────┬───────────────────────┬──────────────────┘
                                 │                       │
                 ┌───────────────▼─────────┐   ┌─────────▼───────────────────┐
                 │ metering.py             │   │ billing.py                  │
                 │ lock tenant row         │   │ Checkout session (test key) │
                 │ idempotency lookup      │   │ webhook: verify signature → │
                 │ quota check → price →   │   │ dedupe (stripe_events PK) → │
                 │ write events + outbox   │   │ stale-event guard → plan    │
                 └───────┬───────────┬─────┘   └─────────▲───────────────────┘
                         │           │                   │ signed POST
              pricing.py (pure,      │                   │ /webhooks/stripe
              pinned constants)      │            ┌──────┴──────┐
                         │           │            │   Stripe    │
                 ┌───────▼───────────▼──────┐     │ (test mode) │
                 │ PostgreSQL (Alembic)     │     └─────────────┘
                 │ tenants · plans ·        │
                 │ subscriptions ·          │◀──── jobs.py worker thread
                 │ usage_events ·           │      claims jobs (SKIP LOCKED),
                 │ idempotency_keys ·       │      retries 2s/4s/8s, then
                 │ stripe_events · jobs ·   │      marks failed + writes alert
                 │ notifications · alerts   │
                 └──────────────────────────┘
```

Design decisions and the non-goal are in [DESIGN.md](DESIGN.md).

## Run it

### Docker (recommended)

```bash
cp .env.example .env          # optional for metering; required for Stripe
docker compose up --build     # Postgres 16 + API on http://localhost:8000; migrations run on start
docker compose exec api python -m app.seed
```

### Without Docker

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # defaults to SQLite (single-user dev only)
alembic upgrade head
python -m app.seed
uvicorn app.main:app --port 8000
```

### Seed data

`python -m app.seed` is idempotent and prints three demo tenants:

| Tenant | API key | Plan | State |
|---|---|---|---|
| Demo Free | `demo-free-key` | free | empty |
| Demo Pro | `demo-pro-key` | pro | active subscription |
| Demo Boundary | `demo-boundary-key` | free | 998 / 1,000 calls used, so call 3 hits the 402 |

```bash
curl -s localhost:8000/usage -H "X-API-Key: demo-boundary-key"
curl -s -X POST localhost:8000/generate -H "X-API-Key: demo-boundary-key" \
     -H "Idempotency-Key: try-1" -H "Content-Type: application/json" -d '{"prompt":"hello"}'
```

### Tests

```bash
pytest                                                   # SQLite: 65 passed, 2 skipped
TEST_DATABASE_URL=postgresql+psycopg2://... pytest       # Postgres: 67 passed (adds the concurrency tests)
```

### Acceptance probes

```bash
BASE_URL=http://localhost:8000 ADMIN_TOKEN=dev-admin-token WEBHOOK_SECRET=whsec_dev_local_only \
  bash scripts/probes.sh
```

Those are the Docker defaults when no `.env` is present. If you set your own `ADMIN_TOKEN` or `STRIPE_WEBHOOK_SECRET`, use those values instead.

Runs the five evaluation probes end to end. Output from a real run is in [scripts/probe_transcript.txt](scripts/probe_transcript.txt).

### Stripe (test mode)

1. Put your `sk_test_...` key in `.env` as `STRIPE_SECRET_KEY`. The app refuses `sk_live_` keys.
2. `python -m app.stripe_setup` creates the Pro product and a $29/month price (idempotent). Copy the printed `STRIPE_PRICE_PRO` into `.env`.
3. `stripe listen --forward-to localhost:8000/webhooks/stripe`. Copy the printed `whsec_...` into `.env` as `STRIPE_WEBHOOK_SECRET`, then restart the API.
4. `curl -X POST localhost:8000/billing/checkout -H "X-API-Key: demo-free-key"`, open the returned `url`, and pay with card `4242 4242 4242 4242`.
5. `curl localhost:8000/usage -H "X-API-Key: demo-free-key"` now shows `plan: pro` and the Pro limits.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | liveness |
| GET | `/plans` | none | plan catalogue |
| POST | `/tenants` | `X-Admin-Token` | create tenant, returns API key once |
| POST | `/generate` | `X-API-Key` + `Idempotency-Key` | the billable action |
| GET | `/usage` | `X-API-Key` | this month's usage, limits, cost |
| GET | `/notifications` | `X-API-Key` | 80% / 100% quota alerts from the worker |
| POST | `/billing/checkout` | `X-API-Key` | start Free → Pro upgrade, returns Checkout URL |
| POST | `/webhooks/stripe` | Stripe signature | plan changes |

## Plans and pricing

| Plan | API calls / month | AI tokens / month | Price |
|---|---|---|---|
| Free | 1,000 | 100,000 | $0 |
| Pro | 50,000 | 5,000,000 | $29 / month |

Token prices are pinned in `app/pricing.py`, modeled on Gemini 2.5 Flash list prices:

| Category | Per 1M tokens | pico-USD per token |
|---|---|---|
| Fresh input | $0.30 | 300,000 |
| Cached input | $0.075 | 75,000 |
| Output | $2.50 | 2,500,000 |
| Reasoning | billed as output | 2,500,000 |
| Each API call | $0.001 flat | 1,000,000,000 per call |

`input_tokens` includes cached tokens, so fresh = input − cached. Quota counts input + output + reasoning.

## Limitations

- **The live Stripe Checkout flow has not been run against Stripe's servers.** It was built without a Stripe account: the build sandbox can't reach `api.stripe.com`. The Checkout call is covered by a test that mocks Stripe's SDK. The webhook path is proven against a running server with events signed exactly the way Stripe signs them (HMAC-SHA256 over `timestamp.payload`, checked by Stripe's own `construct_event`). The steps above close that gap.
- **`docker compose up` was not run during the build** (the sandbox had no Docker daemon). The same migrations, seed and server were run against a real PostgreSQL 16 instance instead.
- **SQLite is for single-user dev only.** Serialising concurrent requests relies on `SELECT ... FOR UPDATE`, which SQLite ignores. The concurrency tests only run on Postgres.
- **Event ordering uses Stripe's `created` timestamp, which has one-second resolution.** Two subscription events in the same second are applied in arrival order.
- **No invoicing, proration or overage billing** (the stated non-goal). Pro over quota is blocked with a 429, not charged.
- **Notifications are stored, not emailed.** The job queue has retries and a failure alert; the delivery channel is a table.
- **Billing periods are calendar months in UTC**, not anniversary dates.
