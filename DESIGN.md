# Design — Usage Metering & Billing Engine

One page. Written before any code (Phase 1 gate).

## Problem

A SaaS backend has to answer three questions for every tenant: how much have they
used, what does it cost, and have they hit their plan limit? The answers have to stay
correct when the network retries a request, when Stripe delivers a webhook twice, and
when a tenant sits exactly on their quota boundary. Every one of those is a way to
overcharge a customer, give away free usage, or lose revenue.

## Plans

| Plan | API calls / month | AI tokens / month | Monthly fee |
|------|------------------:|------------------:|------------:|
| Free | 1,000 | 100,000 | $0 |
| Pro  | 50,000 | 5,000,000 | $29 |

Periods are calendar months in UTC. Plans live in a `plans` table seeded by the
first migration, so limits are data, not code.

## Data model

```
plans(code PK, name, api_calls_limit, ai_tokens_limit, monthly_fee_cents)
tenants(id PK, name, api_key_hash UNIQUE, plan_code FK, subscription_status,
        stripe_customer_id, stripe_subscription_id, stripe_state_at)
subscriptions(id PK, tenant_id FK, stripe_subscription_id UNIQUE, status,
              plan_code, current_period_end, updated_at)
usage_events(id PK, tenant_id FK, type, quantity, input_tokens,
             cached_input_tokens, output_tokens, reasoning_tokens,
             cost_pico, idempotency_key, period, created_at)
    UNIQUE (tenant_id, idempotency_key, type)       -- DB-level no-double-count
    INDEX  (tenant_id, period, type)                -- rollups
idempotency_keys(tenant_id, key, request_hash, response_code, response_body)
    UNIQUE (tenant_id, key)
stripe_events(event_id PK, type, received_at)       -- webhook dedup
jobs(id, type, payload, status, attempts, max_attempts, run_after, last_error)
notifications(tenant_id, metric, threshold, period, message)
    UNIQUE (tenant_id, metric, threshold, period)   -- each alert fires once
alerts(id, source, message, created_at)             -- ops: failed jobs land here
```

Every tenant-owned row carries `tenant_id`, and every query that reads tenant data
filters on it. API keys are stored as SHA-256 hashes, never in plain text.

## Money

All money is integers. Costs are stored in **pico-USD** (10⁻¹² USD) because that unit
makes every per-token price a whole number: $0.30 per million input tokens is exactly
300,000 pico-USD per token. Events store their exact cost; rollups sum the exact
integers and round **once**, at the end. Rounding per event would lose money: a
thousand 0.3 µUSD events must total 300 µUSD, not zero.

## Token pricing rules

Prices are pinned in `app/pricing.py`, modeled on Gemini 2.5 Flash list prices.

- `input_tokens` is the provider's total prompt count and **includes** cached tokens.
- `cached_input_tokens` is the cached subset (must be ≤ input), billed at 25% of input.
- `output_tokens` excludes reasoning; `reasoning_tokens` are billed **at the output rate**.
- cost = (input − cached)·P_in + cached·P_cached + (output + reasoning)·P_out
- quota tokens = input + output + reasoning (cached counted once, inside input)

The categories cannot simply be added: `input + cached` counts the cached tokens
twice, and pricing everything at one rate overcharges cache hits and undercharges
reasoning.

## API surface

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | /health | — | liveness |
| GET | /plans | — | plan catalogue |
| POST | /tenants | admin token | create tenant, returns API key once |
| POST | /generate | tenant key + `Idempotency-Key` | the billable action |
| GET | /usage | tenant key | used / limit / cost for the period |
| GET | /notifications | tenant key | 80% / 100% quota alerts |
| POST | /billing/checkout | tenant key | Stripe Checkout session for Pro |
| POST | /webhooks/stripe | Stripe signature | plan sync |

## Idempotency strategy

`POST /generate` requires an `Idempotency-Key` header, scoped per tenant. The whole
request runs in one transaction that first locks the tenant row:

1. Key already stored with the same body hash → return the stored response verbatim
   (`Idempotent-Replayed: true`). No new events.
2. Same key, different body → `409 Conflict`.
3. Otherwise check quota, insert usage events, store the response under the key, commit.

Only successful outcomes are stored. A request rejected for quota leaves no record,
so the client can retry the same key after upgrading. The unique constraint on
`usage_events` is a second line of defence if application logic ever fails.

## Quota rule (the boundary)

A request is allowed when `used + requested ≤ limit` for **both** metrics. At 999 of
1,000 calls, one more call is allowed (1,000 ≤ 1,000); the call after that is rejected.
No partial fulfilment.

| Situation | Status | Why |
|-----------|--------|-----|
| Free tenant over quota | **402** | the fix is to pay — upgrade to Pro |
| Pro tenant over quota | **429** + `Retry-After` | already on the top plan; wait for reset |
| Pro tenant with lapsed payment | **402** | payment problem, blocked before quota |

Every rejection names the metric, used, requested, limit, and reset time.

## Layers

```
HTTP     app/main.py          routes, auth, status codes, validation (Pydantic)
Logic    app/metering.py      record + idempotency + quota
         app/pricing.py       money math
         app/billing.py       Stripe checkout + webhook sync
         app/jobs.py          background queue + worker
Data     app/models.py, app/db.py, migrations/
```

Routes never touch SQL; services never know about HTTP.

## Background job

Crossing 80% or 100% of a quota enqueues a `usage_threshold_alert` job **in the same
transaction** as the usage write (an outbox), so the alert cannot be lost or sent for
a write that rolled back. A worker thread claims jobs, retries failures with
exponential backoff, and on final failure marks the job failed and writes an `alerts`
row plus an ERROR log line.

## Non-goal

**No invoicing, proration, or overage billing.** Over the limit means rejected, not
charged extra. Payment truth lives at Stripe; this service mirrors plan state from
verified webhooks only and never moves money itself.
