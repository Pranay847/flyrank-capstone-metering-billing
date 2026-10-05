"""Checkout session creation. Stripe's API is faked here (no network in tests); the
real test-mode run is documented in EVIDENCE.md."""

from types import SimpleNamespace

import stripe

from helpers import make_tenant


def test_checkout_without_stripe_configured_is_a_clear_503(client):
    _, key = make_tenant(client)
    r = client.post("/billing/checkout", headers={"X-API-Key": key})
    assert r.status_code == 503 and r.json()["error"]["code"] == "stripe_not_configured"


def test_live_keys_are_refused(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_never")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_x")
    _, key = make_tenant(client)
    r = client.post("/billing/checkout", headers={"X-API-Key": key})
    assert r.status_code == 503 and r.json()["error"]["code"] == "stripe_live_key_refused"


def test_checkout_session_carries_the_tenant_everywhere_stripe_will_echo_it(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro_test")
    seen = {}

    def fake_create(**params):
        seen.update(params)
        return SimpleNamespace(id="cs_test_123", url="https://checkout.stripe.com/c/pay/cs_test_123")

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)
    tid, key = make_tenant(client)
    r = client.post("/billing/checkout", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json() == {"checkout_url": "https://checkout.stripe.com/c/pay/cs_test_123",
                        "session_id": "cs_test_123"}
    assert seen["mode"] == "subscription"
    assert seen["line_items"] == [{"price": "price_pro_test", "quantity": 1}]
    assert seen["client_reference_id"] == tid
    assert seen["metadata"] == {"tenant_id": tid}
    assert seen["subscription_data"] == {"metadata": {"tenant_id": tid}}
    assert seen["api_key"] == "sk_test_fake"


def test_already_pro_cannot_check_out_again(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro_test")
    _, key = make_tenant(client, plan="pro", status="active")
    assert client.post("/billing/checkout", headers={"X-API-Key": key}).status_code == 409
