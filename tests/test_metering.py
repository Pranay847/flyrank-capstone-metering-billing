"""Exactly-once metering via Idempotency-Key."""

from helpers import SMALL, event_count, gen, make_tenant, preload, set_plan

BODY = {"prompt": "hello", "usage": {"input_tokens": 10_000, "cached_input_tokens": 4_000,
                                     "output_tokens": 2_000, "reasoning_tokens": 3_000}}


def test_same_request_twice_records_exactly_one_event(client):
    tid, key = make_tenant(client)
    r1 = gen(client, key, "req-001", BODY)
    r2 = gen(client, key, "req-001", BODY)

    assert r1.status_code == r2.status_code == 201
    assert r1.headers.get("Idempotent-Replayed") is None
    assert r2.headers["Idempotent-Replayed"] == "true"
    assert r1.json() == r2.json()                      # the retry mirrors the original
    assert event_count(tid, "req-001") == {"api_call": 1, "ai_tokens": 1}

    usage = client.get("/usage", headers={"X-API-Key": key}).json()
    assert usage["api_calls"]["used"] == 1
    assert usage["ai_tokens"]["used"] == 15_000
    assert usage["cost"]["total_pico_usd"] == 15_600_000_000   # call + tokens
    assert usage["cost"]["total_display"] == "$0.015600"


def test_ten_retries_still_one_event(client):
    tid, key = make_tenant(client)
    for _ in range(10):
        assert gen(client, key, "retry-storm", BODY).status_code == 201
    assert event_count(tid) == {"api_call": 1, "ai_tokens": 1}


def test_same_key_different_body_is_rejected(client):
    tid, key = make_tenant(client)
    gen(client, key, "k1", BODY)
    r = gen(client, key, "k1", {"prompt": "something else"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_key_reused"
    assert event_count(tid) == {"api_call": 1, "ai_tokens": 1}


def test_missing_or_malformed_idempotency_key(client):
    _, key = make_tenant(client)
    r = client.post("/generate", json=SMALL, headers={"X-API-Key": key})
    assert r.status_code == 400 and r.json()["error"]["code"] == "missing_idempotency_key"
    r = gen(client, key, "has spaces!", SMALL)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_idempotency_key"


def test_keys_are_scoped_per_tenant(client):
    a, ka = make_tenant(client, "A")
    b, kb = make_tenant(client, "B")
    assert gen(client, ka, "shared-key").status_code == 201
    r = gen(client, kb, "shared-key")
    assert r.status_code == 201 and r.headers.get("Idempotent-Replayed") is None
    assert event_count(a) == event_count(b) == {"api_call": 1, "ai_tokens": 1}


def test_rejected_request_is_not_stored_so_the_key_works_after_upgrading(client):
    tid, key = make_tenant(client)
    preload(tid, "api_call", 1_000)
    assert gen(client, key, "after-upgrade").status_code == 402
    set_plan(tid, "pro", "active")
    r = gen(client, key, "after-upgrade")
    assert r.status_code == 201 and r.headers.get("Idempotent-Replayed") is None


def test_default_token_counts_are_deterministic(client):
    _, key = make_tenant(client)
    r = gen(client, key, "no-usage", {"prompt": "a" * 400})
    assert r.json()["metered"]["token_breakdown"]["input_tokens"] == 100
    assert r.json()["metered"]["token_breakdown"]["output_tokens"] == 200
