# Evidence

One proof per requirement in Section 6 of the brief. Everything below was pasted from real runs. Live outputs come from `scripts/probes.sh` against `uvicorn` + PostgreSQL 16 with the background worker running. The full transcript is in [scripts/probe_transcript.txt](scripts/probe_transcript.txt).

**Test suite**

```
$ pytest -q                                   # SQLite
65 passed, 2 skipped, 1 warning in 12.49s
$ TEST_DATABASE_URL=postgresql://... pytest -q   # PostgreSQL 16
67 passed, 1 warning in 5.26s
```

The 2 skips are the concurrency tests. They need Postgres row locks, so they only run there.

---

## 1. Metering: each billable request is recorded exactly once, retries don't double count

**Probe 1** (live): the same request sent twice with one `Idempotency-Key`.

```
--- first request
HTTP 201
  "event_ids": [2, 3],
  "recorded_at": "2026-10-05T03:15:16.126830+00:00"
--- retry, same key
HTTP 201
idempotent-replayed: true
  "event_ids": [2, 3],            <- same events, identical body
  "recorded_at": "2026-10-05T03:15:16.126830+00:00"
--- usage after both
api_calls used = 1
```

How: `metering.record_billable` locks the tenant row (`SELECT ... FOR UPDATE`, app/metering.py:146). It then looks up `(tenant_id, key)` in `idempotency_keys`. A hit with the same body hash replays the stored response. A hit with a different body is a 409. A unique constraint `uq_usage_once (tenant_id, idempotency_key, type)` on `usage_events` backs this up if the app logic is ever bypassed.

**Under concurrency** (Postgres): `test_concurrent_retries_of_one_key_record_one_event` fires 20 threads through a barrier with the same key. Result: exactly one event. To check the test can actually fail, the row lock was removed temporarily. It failed with `assert 14 == 1` (14 events double-counted). The lock was then restored.

Other tests: `test_ten_retries_still_one_event`, `test_same_key_different_body_is_rejected` (409), `test_keys_are_scoped_per_tenant`, `test_rejected_request_is_not_stored_so_the_key_works_after_upgrading`.

## 2. Quota is checked before the request is recorded, and over-quota requests are rejected

**Probe 2** (live): fill the Free token quota exactly, then ask for one more token.

```
--- one request for exactly the Free token quota (100,000 tokens)
HTTP 201
    "ai_tokens": { "used": 100000, "limit": 100000, "remaining": 0, "percent": 100.0 }
--- one more token after the boundary
HTTP 402
```

Rule: a request is admitted only if `used + requested <= limit` for **both** API calls and tokens. The check runs inside the tenant lock, before any insert. A rejected request writes nothing: no event, no idempotency row, no job (`test_a_rejected_request_enqueues_nothing`).

At the exact boundary under concurrency: `test_concurrent_requests_at_the_boundary_admit_exactly_one` puts a tenant at 999/1,000 calls and fires 20 concurrent requests. Exactly 1 gets a 201 and 19 get a 402.

Other tests: `test_free_api_call_boundary_exact` (call 1,000 → 201, call 1,001 → 402), `test_free_token_boundary_exact`, `test_request_that_would_overshoot_is_rejected_whole`, `test_both_metrics_are_checked`, `test_usage_resets_in_a_new_period`.

## 3. 429 / 402 with clear messages

Free over quota → **402**. The real body from Probe 2:

```json
{
  "error": {
    "code": "quota_exceeded_upgrade_required",
    "message": "This request needs 1 AI token but your Free plan has 0 of 100,000 left this month. Upgrade to Pro (POST /billing/checkout) to continue now, or wait for the reset.",
    "metric": "ai_tokens", "used": 100000, "requested": 1, "limit": 100000, "remaining": 0,
    "plan": "free", "resets_at": "2026-11-01T00:00:00+00:00", "upgrade": "/billing/checkout"
  }
}
```

| Situation | Status | Code | Extra |
|---|---|---|---|
| Free plan, over quota | 402 | `quota_exceeded_upgrade_required` | upgrade link |
| Pro plan, over quota | 429 | `quota_exceeded` | `Retry-After` = seconds to next UTC month |
| Pro, payment `past_due` / `unpaid` / `incomplete` | 402 | `payment_required` | even with quota left |

Tests: `test_pro_over_quota_gets_429_with_retry_after` (asserts `Retry-After` = 1,425,600 s, which is 16.5 days from the frozen test clock), `test_lapsed_payment_gets_402_even_with_quota_left`, `test_past_due_subscription_blocks_billable_calls_with_402`.

## 4. Monthly rollup and cost

`GET /usage` sums `usage_events` for the tenant and current period, using index `ix_usage_rollup (tenant_id, period, type)`. Each event's cost is stored in integer pico-USD. Rounding happens once, on the total (`test_rounding_happens_once_at_the_rollup_not_per_event`).

From Probe 5 (live):

```json
GET /usage -> "cost": {
  "api_calls_pico_usd": 1000000000,
  "ai_tokens_pico_usd": 14600000000,
  "total_pico_usd": 15600000000,
  "total_micro_usd": 15600,
  "total_cents": 2,
  "total_display": "$0.015600"
}
```

## 5. Token pricing: cached input is cheaper, reasoning is billed as output, categories aren't simply added

**Probe 5** (live): input 10,000 tokens (4,000 of them cached), output 2,000, reasoning 3,000.

```
fresh     6,000 × 300,000    =  1,800,000,000
cached    4,000 ×  75,000    =    300,000,000
output    2,000 × 2,500,000  =  5,000,000,000
reasoning 3,000 × 2,500,000  =  7,500,000,000
                                --------------
tokens                          14,600,000,000 pico-USD
+ 1 API call                     1,000,000,000
= total                         15,600,000,000 pico-USD = $0.015600
```

What the server returned:

```json
"token_breakdown": { "input_tokens": 10000, "cached_input_tokens": 4000, "fresh_input_tokens": 6000,
                     "output_tokens": 2000, "reasoning_tokens": 3000 },
"cost": { "api_call_pico_usd": 1000000000, "ai_tokens_pico_usd": 14600000000,
          "total_pico_usd": 15600000000, "total_display": "$0.015600" }
```

The total matches the hand calculation, and `/usage` (box 4) matches the response.

"Not simply added": cached tokens are a subset of `input_tokens`, so adding every field together double-counts the cache. `test_categories_cannot_simply_be_added` uses the same request: the naive sum is 19,000 tokens against the correct 15,000. Pricing all of them at the input rate gives 5,700,000,000 pico-USD against the correct 14,600,000,000. Cached tokens are charged once, at the cached rate. `cached > input` is a 422.

## 6. Pricing constants are pinned in config

`app/pricing.py`:

```python
INPUT_PICO_PER_TOKEN = 300_000             # $0.30   / 1M fresh input tokens
CACHED_INPUT_PICO_PER_TOKEN = 75_000       # $0.075  / 1M cached input (25% of input)
OUTPUT_PICO_PER_TOKEN = 2_500_000          # $2.50   / 1M output tokens
REASONING_PICO_PER_TOKEN = OUTPUT_PICO_PER_TOKEN  # reasoning IS output: same price, by rule
API_CALL_PICO = 1_000_000_000              # $0.001  per billable API call
```

`test_pinned_price_constants` fails if any of these change without the test being updated. Integer pico-USD (10⁻¹² USD) keeps every per-token price a whole number, so there's no float drift.

## 7. Stripe Checkout end to end

**What was verified, and what wasn't:**

- `POST /billing/checkout` builds a subscription-mode Checkout Session. The tenant id goes in `client_reference_id`, `metadata` and `subscription_data.metadata`, and an existing Stripe customer is reused. `test_checkout_session_carries_the_tenant_everywhere_stripe_will_echo_it` checks the exact arguments sent to the Stripe SDK (mocked). Other tests: `test_live_keys_are_refused`, `test_checkout_without_stripe_configured_is_a_clear_503`, `test_already_pro_cannot_check_out_again`.
- **Probe 3** (live): a `checkout.session.completed` event, signed the way Stripe signs it, flips the tenant to Pro and `/usage` shows the new limits:

```
--- before
plan=free api_calls_limit=1000 ai_tokens_limit=100000
--- signed checkout.session.completed
{"status":"processed","event_id":"evt_p3_...","type":"checkout.session.completed","result":"tenant_upgraded_to_pro"}
HTTP 200
--- after
plan=pro status=active api_calls_limit=50000 ai_tokens_limit=5000000
```

- **Not yet done:** a real hosted Checkout payment with a Stripe test account. That needs a `sk_test_` key, and the build sandbox couldn't reach `api.stripe.com`. The README's Stripe section lists the five steps; this box gets its final screenshot once that run is done.

## 8. Webhooks: signature verified, duplicates ignored, plan updated

**Probe 4** (live):

```
--- forged (signed with the wrong secret)
{"error":{"code":"invalid_signature","message":"Webhook signature verification failed."}}
HTTP 400
plan after forgery = free
--- real event, delivery 1
{"status":"processed","event_id":"evt_p4_...","type":"checkout.session.completed","result":"tenant_upgraded_to_pro"}
HTTP 200
--- real event, delivery 2 (replay)
{"status":"duplicate_ignored","event_id":"evt_p4_...","type":"checkout.session.completed"}
HTTP 200
```

How (app/billing.py):

- Verify: `stripe.Webhook.construct_event(payload, sig_header, secret)` runs on the raw request bytes, with a 300 s replay window.
- Dedupe: the event id is inserted into `stripe_events` (primary key) in the same transaction as the plan change. A duplicate delivery hits the PK and returns `duplicate_ignored`.
- Out of order: `tenant.stripe_state_at` stores the newest `event.created` applied. Older events are ignored.
- Handled: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`.

Tests (12): `test_forged_signature_is_rejected_and_changes_nothing`, `test_missing_signature_is_rejected`, `test_old_signature_is_rejected_replay_window`, `test_replayed_event_is_processed_exactly_once`, `test_subscription_deleted_downgrades_to_free`, `test_out_of_order_stale_event_is_ignored`, `test_unpaid_checkout_does_not_grant_pro`, `test_subscription_event_before_checkout_finds_tenant_by_metadata`, `test_signed_but_invalid_json_is_400`, and others.

## 9. Database: tenants, plans, subscriptions, usage events, isolated tenants

The schema is owned by Alembic (`migrations/versions/0001_initial.py`). The app never calls `create_all()`.

| Table | Key constraint / index |
|---|---|
| `tenants` | `api_key_hash` unique (keys stored as SHA-256, never plaintext) |
| `plans` | seeded by the migration (free, pro) |
| `subscriptions` | Stripe subscription per tenant |
| `usage_events` | `uq_usage_once (tenant_id, idempotency_key, type)`, `ix_usage_rollup (tenant_id, period, type)` |
| `idempotency_keys` | `uq_idem_tenant_key (tenant_id, key)` |
| `stripe_events` | `event_id` primary key (webhook dedupe) |
| `jobs` | `ix_jobs_due (status, run_after)` |
| `notifications` | `uq_notify_once (tenant_id, metric, threshold, period)` |
| `alerts` | permanent job failures |

```
$ alembic upgrade head; echo "exit $?"
exit 0
$ alembic current
0001_initial (head)
```

The same migration ran against PostgreSQL 16 before the probe run and before the 67-test Postgres suite.

Isolation: every query is scoped by the tenant resolved from the API key. `test_tenants_never_see_each_others_usage` and `test_keys_are_scoped_per_tenant` (the same idempotency key in two tenants makes two separate events) cover it. `test_api_keys_are_stored_hashed` checks no plaintext key is stored.

## 10. Background job with retries and a failure alert

From Probe 2 (live): the token fill crossed 80% and 100% in one request. The worker thread picked up both outbox jobs and wrote:

```json
[
  { "metric": "ai_tokens", "threshold": 80,  "period": "2026-10",
    "message": "You have reached 80% of your Free plan's monthly AI tokens for 2026-10. Current usage: 100,000 of 100,000.",
    "created_at": "2026-10-05T03:15:16.905223+00:00" },
  { "metric": "ai_tokens", "threshold": 100, "period": "2026-10",
    "message": "You have reached 100% of your Free plan's monthly AI tokens for 2026-10. Current usage: 100,000 of 100,000.",
    "created_at": "2026-10-05T03:15:16.940160+00:00" }
]
```

`test_failing_job_retries_with_backoff_then_raises_an_alert`: attempt 1 fails → retry after 2 s (not picked up early) → attempt 2 after 4 s → attempt 3 fails → `status = failed`, an `alerts` row reading "failed permanently after 3 attempts", and an ERROR log line. `test_alert_is_sent_only_once_even_if_the_job_runs_twice` shows retries can't send duplicate notifications.

## 11. Validation at the boundary: bad input is a 4xx, never a 500

```
test_bad_usage_is_a_clean_422        (negative, float, string, bool, cached > input, > 10M, unknown field, missing field)
test_bad_prompt_is_a_clean_422       (missing, empty, > 10,000 chars, wrong type)
test_malformed_json_is_a_clean_422
test_missing_or_malformed_idempotency_key   -> 400
test_auth                                    -> 401
```

All errors share one shape: `{"error": {"code", "message", ...}}`. Unexpected exceptions are logged with a stack trace and the client gets a generic `internal_error` with no internals.

## 12. README, diagram, setup, required files

- [README.md](README.md): what it does, architecture diagram, exact run and seed steps, Stripe steps, limitations
- [DESIGN.md](DESIGN.md): Phase 1 design doc with one explicit non-goal
- [capstone.yaml](capstone.yaml): run / seed / test / base_url / endpoints
- [BUILDLOG.md](BUILDLOG.md): AI-usage log
- [.env.example](.env.example): every variable the app reads. No real secret is committed; `.env` is git-ignored.
- Commit history: one commit per phase.

Seed check (live, after `python -m app.seed`):

```
GET /usage (demo-boundary-key) -> api_calls: {'used': 998, 'limit': 1000, 'remaining': 2, 'percent': 99.8}
```
