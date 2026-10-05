"""Tenant isolation, auth, and boundary validation (bad input -> 4xx, never 500)."""

import pytest

from helpers import ADMIN, gen, make_tenant


def test_tenants_never_see_each_others_usage(client):
    _, ka = make_tenant(client, "A")
    _, kb = make_tenant(client, "B")
    for i in range(3):
        gen(client, ka, f"a-{i}")
    assert client.get("/usage", headers={"X-API-Key": ka}).json()["api_calls"]["used"] == 3
    assert client.get("/usage", headers={"X-API-Key": kb}).json()["api_calls"]["used"] == 0


def test_auth(client):
    assert client.get("/usage").status_code == 401
    assert client.get("/usage", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.post("/tenants", json={"name": "x"}).status_code == 401
    assert client.post("/tenants", json={"name": "x"}, headers={"X-Admin-Token": "bad"}).status_code == 401


def test_api_keys_are_stored_hashed(client):
    from helpers import tenant
    tid, key = make_tenant(client)
    assert key not in tenant(tid).api_key_hash and len(tenant(tid).api_key_hash) == 64


@pytest.mark.parametrize("usage", [
    {"input_tokens": -1, "output_tokens": 0},                          # negative
    {"input_tokens": 1.5, "output_tokens": 0},                         # float
    {"input_tokens": "10", "output_tokens": 0},                        # string
    {"input_tokens": True, "output_tokens": 0},                        # bool
    {"input_tokens": 10, "output_tokens": 0, "cached_input_tokens": 11},  # cached > input
    {"input_tokens": 10_000_001, "output_tokens": 0},                  # absurd
    {"input_tokens": 10, "output_tokens": 0, "surprise": 1},           # unknown field
    {"output_tokens": 0},                                              # missing field
])
def test_bad_usage_is_a_clean_422(client, usage):
    _, key = make_tenant(client)
    r = gen(client, key, "bad", {"prompt": "x", "usage": usage})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("body", [{}, {"prompt": ""}, {"prompt": "x" * 10_001}, {"prompt": 5}])
def test_bad_prompt_is_a_clean_422(client, body):
    _, key = make_tenant(client)
    assert gen(client, key, "bad", body).status_code == 422


def test_malformed_json_is_a_clean_422(client):
    _, key = make_tenant(client)
    r = client.post("/generate", content="{not json",
                    headers={"X-API-Key": key, "Idempotency-Key": "k", "Content-Type": "application/json"})
    assert r.status_code == 422


def test_plans_catalogue(client):
    plans = {p["code"]: p for p in client.get("/plans").json()}
    assert plans["free"]["api_calls_limit"] == 1_000 and plans["free"]["ai_tokens_limit"] == 100_000
    assert plans["pro"]["api_calls_limit"] == 50_000 and plans["pro"]["monthly_fee_cents"] == 2_900
