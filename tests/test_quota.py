"""Quota enforcement at the exact boundary, with honest status codes."""

from helpers import event_count, gen, make_tenant, preload

# Frozen clock: 2026-10-15T12:00Z. Reset is 2026-11-01T00:00Z = 16.5 days later.
SECONDS_TO_RESET = int(16.5 * 24 * 3600)


def test_free_api_call_boundary_exact(client):
    tid, key = make_tenant(client)
    preload(tid, "api_call", 999)

    at_limit = gen(client, key, "call-1000")
    assert at_limit.status_code == 201                       # 999 + 1 = 1000 <= 1000
    assert at_limit.json()["usage_after"]["api_calls"] == {
        "used": 1000, "limit": 1000, "remaining": 0, "percent": 100.0}

    over = gen(client, key, "call-1001")
    assert over.status_code == 402                           # Free: the fix is to upgrade
    err = over.json()["error"]
    assert err["code"] == "quota_exceeded_upgrade_required"
    assert (err["metric"], err["used"], err["requested"], err["limit"], err["remaining"]) == \
           ("api_calls", 1000, 1, 1000, 0)
    assert err["upgrade"] == "/billing/checkout"
    assert "Upgrade to Pro" in err["message"]
    assert event_count(tid, "call-1001") == {}               # nothing recorded


def test_free_token_boundary_exact(client):
    tid, key = make_tenant(client)
    preload(tid, "ai_tokens", 99_000)
    fill = {"prompt": "x", "usage": {"input_tokens": 600, "output_tokens": 400}}  # 1,000 tokens
    assert gen(client, key, "fill", fill).status_code == 201  # exactly 100,000
    r = gen(client, key, "one-more")
    assert r.status_code == 402 and r.json()["error"]["metric"] == "ai_tokens"


def test_request_that_would_overshoot_is_rejected_whole(client):
    tid, key = make_tenant(client)
    preload(tid, "ai_tokens", 99_000)
    big = {"prompt": "x", "usage": {"input_tokens": 1_001, "output_tokens": 0}}
    r = gen(client, key, "overshoot", big)
    assert r.status_code == 402
    assert r.json()["error"]["requested"] == 1_001 and r.json()["error"]["remaining"] == 1_000
    usage = client.get("/usage", headers={"X-API-Key": key}).json()
    assert usage["ai_tokens"]["used"] == 99_000              # no partial fulfilment


def test_pro_over_quota_gets_429_with_retry_after(client):
    tid, key = make_tenant(client, plan="pro", status="active")
    preload(tid, "api_call", 50_000)
    r = gen(client, key, "pro-over")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == str(SECONDS_TO_RESET)
    assert r.json()["error"]["code"] == "quota_exceeded"
    assert r.json()["error"]["resets_at"] == "2026-11-01T00:00:00+00:00"


def test_lapsed_payment_gets_402_even_with_quota_left(client):
    tid, key = make_tenant(client, plan="pro", status="past_due")
    r = gen(client, key, "lapsed")
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "payment_required"
    assert event_count(tid) == {}


def test_both_metrics_are_checked(client):
    tid, key = make_tenant(client)
    preload(tid, "ai_tokens", 100_000)          # tokens full, calls nearly empty
    r = gen(client, key, "tokens-full")
    assert r.status_code == 402 and r.json()["error"]["metric"] == "ai_tokens"


def test_usage_resets_in_a_new_period(client):
    from datetime import datetime, timezone
    from app import clock
    tid, key = make_tenant(client)
    preload(tid, "api_call", 1_000)
    assert gen(client, key, "oct").status_code == 402
    clock.freeze(datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc))
    assert gen(client, key, "nov").status_code == 201
