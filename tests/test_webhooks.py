"""Stripe webhooks: signature first, dedup second, order-safe, plan mirrored."""

import time

from sqlalchemy import func, select

from app.db import session
from app.models import StripeEvent, Subscription
from helpers import (checkout_event, gen, make_tenant, post_event, signed,
                     subscription_event, tenant)


def stripe_event_rows() -> int:
    with session() as db:
        return db.execute(select(func.count()).select_from(StripeEvent)).scalar_one()


def test_forged_signature_is_rejected_and_changes_nothing(client):
    tid, key = make_tenant(client)
    r = post_event(client, checkout_event(tid), secret="whsec_attacker")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_signature"
    assert tenant(tid).plan_code == "free"
    assert stripe_event_rows() == 0


def test_missing_signature_is_rejected(client):
    tid, _ = make_tenant(client)
    payload, _ = signed(checkout_event(tid))
    r = client.post("/webhooks/stripe", content=payload, headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_old_signature_is_rejected_replay_window(client):
    tid, _ = make_tenant(client)
    r = post_event(client, checkout_event(tid), ts=int(time.time()) - 600)  # 10 min old
    assert r.status_code == 400 and tenant(tid).plan_code == "free"


def test_checkout_completed_flips_tenant_to_pro_and_usage_shows_new_limits(client):
    tid, key = make_tenant(client)
    r = post_event(client, checkout_event(tid))
    assert r.status_code == 200 and r.json()["result"] == "tenant_upgraded_to_pro"
    t = tenant(tid)
    assert (t.plan_code, t.subscription_status, t.stripe_customer_id, t.stripe_subscription_id) == \
           ("pro", "active", "cus_test_1", "sub_test_1")
    usage = client.get("/usage", headers={"X-API-Key": key}).json()
    assert usage["plan"]["code"] == "pro"
    assert usage["api_calls"]["limit"] == 50_000 and usage["ai_tokens"]["limit"] == 5_000_000


def test_replayed_event_is_processed_exactly_once(client):
    tid, _ = make_tenant(client)
    evt = checkout_event(tid)
    first, second = post_event(client, evt), post_event(client, evt)
    assert first.json()["status"] == "processed"
    assert second.status_code == 200 and second.json()["status"] == "duplicate_ignored"
    assert stripe_event_rows() == 1
    with session() as db:
        assert db.execute(select(func.count()).select_from(Subscription)).scalar_one() == 1


def test_past_due_subscription_blocks_billable_calls_with_402(client):
    tid, key = make_tenant(client)
    post_event(client, checkout_event(tid))
    post_event(client, subscription_event(tid, "customer.subscription.updated", "past_due", "evt_pd"))
    assert tenant(tid).subscription_status == "past_due"
    r = gen(client, key, "after-lapse")
    assert r.status_code == 402 and r.json()["error"]["code"] == "payment_required"


def test_subscription_deleted_downgrades_to_free(client):
    tid, _ = make_tenant(client)
    post_event(client, checkout_event(tid))
    r = post_event(client, subscription_event(tid, "customer.subscription.deleted", "canceled", "evt_del"))
    assert r.json()["result"] == "tenant_downgraded_to_free"
    assert (tenant(tid).plan_code, tenant(tid).subscription_status) == ("free", "canceled")


def test_out_of_order_stale_event_is_ignored(client):
    tid, _ = make_tenant(client)
    now = int(time.time())
    post_event(client, checkout_event(tid, created=now - 100))
    post_event(client, subscription_event(tid, "customer.subscription.deleted", "canceled",
                                          "evt_new", created=now))
    late = post_event(client, subscription_event(tid, "customer.subscription.updated", "active",
                                                 "evt_old", created=now - 50))
    assert late.json()["result"] == "stale_ignored"
    assert tenant(tid).plan_code == "free"  # the newer cancellation wins


def test_subscription_event_before_checkout_finds_tenant_by_metadata(client):
    tid, _ = make_tenant(client)
    r = post_event(client, subscription_event(tid, "customer.subscription.created", "active", "evt_early"))
    assert r.json()["result"] == "tenant_now_pro_active"
    assert tenant(tid).stripe_subscription_id == "sub_test_1"


def test_unpaid_checkout_does_not_grant_pro(client):
    tid, _ = make_tenant(client)
    r = post_event(client, checkout_event(tid, payment_status="unpaid"))
    assert r.json()["result"] == "awaiting_payment"
    assert tenant(tid).plan_code == "free"


def test_unhandled_event_types_are_acknowledged(client):
    r = post_event(client, {"id": "evt_x", "object": "event", "type": "invoice.created",
                            "created": int(time.time()), "data": {"object": {"id": "in_1"}}})
    assert r.status_code == 200 and r.json()["result"] == "ignored_event_type"


def test_signed_but_invalid_json_is_400(client):
    import hashlib, hmac
    ts = int(time.time())
    body = "not json"
    sig = hmac.new(b"whsec_test_secret", f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    r = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    assert r.status_code == 400
