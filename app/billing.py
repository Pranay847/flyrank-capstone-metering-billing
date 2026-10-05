"""Stripe, test mode only.

Payment truth lives at Stripe. This service never decides a tenant has paid; it
mirrors plan state from webhook events, and only after:
  1. verifying the Stripe signature (forged or missing -> 400, nothing changes),
  2. deduplicating by event id (a replayed event is acknowledged, not re-applied),
  3. ignoring stale events (Stripe does not guarantee delivery order).
The dedup row and the plan change commit in ONE transaction, so an event is either
fully applied once or not at all.
"""

from __future__ import annotations

import logging

import stripe
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import clock
from .config import get_settings
from .errors import ApiError
from .models import StripeEvent, Subscription, Tenant

log = logging.getLogger("billing.stripe")

# Stripe subscription status -> (plan, our subscription_status)
STATUS_MAP = {
    "active": ("pro", "active"),
    "trialing": ("pro", "active"),
    "past_due": ("pro", "past_due"),      # still Pro, but billable calls get 402
    "unpaid": ("pro", "unpaid"),
    "incomplete": ("pro", "incomplete"),
    "incomplete_expired": ("free", "canceled"),
    "canceled": ("free", "canceled"),
    "paused": ("free", "paused"),
}


# --- Checkout ----------------------------------------------------------------

def _require_test_key() -> str:
    s = get_settings()
    if not s.stripe_secret_key or not s.stripe_price_pro:
        raise ApiError(503, "stripe_not_configured",
                       "Set STRIPE_SECRET_KEY (sk_test_...) and STRIPE_PRICE_PRO in .env. "
                       "Run `python -m app.stripe_setup` to create the Pro price in test mode.")
    if not s.stripe_secret_key.startswith("sk_test_"):
        raise ApiError(503, "stripe_live_key_refused",
                       "This service only runs in Stripe test mode. Use an sk_test_ key.")
    return s.stripe_secret_key


def create_checkout_session(tenant: Tenant) -> dict:
    key = _require_test_key()
    s = get_settings()
    if tenant.plan_code == "pro" and tenant.subscription_status == "active":
        raise ApiError(409, "already_pro", "This tenant is already on an active Pro plan.")
    params = {
        "mode": "subscription",
        "line_items": [{"price": s.stripe_price_pro, "quantity": 1}],
        "success_url": f"{s.public_base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{s.public_base_url}/billing/cancel",
        "client_reference_id": tenant.id,
        "metadata": {"tenant_id": tenant.id},
        # Copied onto the subscription, so subscription events can find the tenant
        # even if they arrive before checkout.session.completed.
        "subscription_data": {"metadata": {"tenant_id": tenant.id}},
    }
    if tenant.stripe_customer_id:
        params["customer"] = tenant.stripe_customer_id
    try:
        session = stripe.checkout.Session.create(api_key=key, **params)
    except stripe.StripeError as exc:
        log.warning("stripe checkout failed: %s", type(exc).__name__)  # never log the key
        raise ApiError(502, "stripe_error", "Stripe could not create a Checkout session. Try again.")
    return {"checkout_url": session.url, "session_id": session.id}


# --- Webhooks ----------------------------------------------------------------

def _lock_tenant(db: Session, *conditions) -> Tenant | None:
    for cond in conditions:
        if cond is None:
            continue
        t = db.execute(select(Tenant).where(cond).with_for_update()).scalar_one_or_none()
        if t is not None:
            return t
    return None


def _tenant_for_subscription(db: Session, sub: dict) -> Tenant | None:
    sid, cust = sub.get("id"), sub.get("customer")
    tid = (sub.get("metadata") or {}).get("tenant_id")
    return _lock_tenant(
        db,
        Tenant.stripe_subscription_id == sid if sid else None,
        Tenant.id == tid if tid else None,
        Tenant.stripe_customer_id == cust if cust else None,
    )


def _is_stale(tenant: Tenant, created: int) -> bool:
    return created < (tenant.stripe_state_at or 0)


def _period_end(sub: dict) -> int | None:
    if sub.get("current_period_end"):
        return int(sub["current_period_end"])
    items = ((sub.get("items") or {}).get("data")) or []
    if items and items[0].get("current_period_end"):  # newer API versions keep it per item
        return int(items[0]["current_period_end"])
    return None


def _upsert_subscription(db: Session, tenant: Tenant, sid: str, status: str, plan: str,
                         period_end: int | None) -> None:
    row = db.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == sid)
    ).scalar_one_or_none()
    now = clock.now()
    if row is None:
        db.add(Subscription(tenant_id=tenant.id, stripe_subscription_id=sid, status=status,
                            plan_code=plan, current_period_end=period_end, updated_at=now))
    else:
        row.status, row.plan_code, row.updated_at = status, plan, now
        if period_end:
            row.current_period_end = period_end


def _on_checkout_completed(db: Session, obj: dict, created: int) -> str:
    if obj.get("mode") != "subscription":
        return "ignored_not_a_subscription"
    tid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("tenant_id")
    tenant = _lock_tenant(db, Tenant.id == tid if tid else None)
    if tenant is None:
        log.warning("checkout.session.completed for unknown tenant %r", tid)
        return "unmatched_tenant"
    if _is_stale(tenant, created):
        return "stale_ignored"
    tenant.stripe_customer_id = obj.get("customer") or tenant.stripe_customer_id
    tenant.stripe_subscription_id = obj.get("subscription") or tenant.stripe_subscription_id
    tenant.stripe_state_at = created
    if obj.get("payment_status") not in ("paid", "no_payment_required"):
        # e.g. a delayed payment method: keep Free until Stripe confirms the subscription.
        tenant.subscription_status = "incomplete"
        return "awaiting_payment"
    tenant.plan_code, tenant.subscription_status = "pro", "active"
    if tenant.stripe_subscription_id:
        _upsert_subscription(db, tenant, tenant.stripe_subscription_id, "active", "pro", None)
    return "tenant_upgraded_to_pro"


def _on_subscription_changed(db: Session, sub: dict, created: int) -> str:
    tenant = _tenant_for_subscription(db, sub)
    if tenant is None:
        log.warning("subscription event for unknown tenant (sub %s)", sub.get("id"))
        return "unmatched_tenant"
    if _is_stale(tenant, created):
        return "stale_ignored"
    plan, status = STATUS_MAP.get(sub.get("status"), (tenant.plan_code, sub.get("status") or "unknown"))
    tenant.stripe_customer_id = sub.get("customer") or tenant.stripe_customer_id
    tenant.stripe_subscription_id = sub.get("id") or tenant.stripe_subscription_id
    tenant.plan_code, tenant.subscription_status, tenant.stripe_state_at = plan, status, created
    if sub.get("id"):
        _upsert_subscription(db, tenant, sub["id"], sub.get("status") or status, plan, _period_end(sub))
    return f"tenant_now_{plan}_{status}"


def _on_subscription_deleted(db: Session, sub: dict, created: int) -> str:
    tenant = _tenant_for_subscription(db, sub)
    if tenant is None:
        return "unmatched_tenant"
    if _is_stale(tenant, created):
        return "stale_ignored"
    tenant.plan_code, tenant.subscription_status, tenant.stripe_state_at = "free", "canceled", created
    if sub.get("id"):
        _upsert_subscription(db, tenant, sub["id"], "canceled", "free", _period_end(sub))
    return "tenant_downgraded_to_free"


HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "customer.subscription.created": _on_subscription_changed,
    "customer.subscription.updated": _on_subscription_changed,
    "customer.subscription.deleted": _on_subscription_deleted,
}


def handle_webhook(db: Session, payload: bytes, sig_header: str | None) -> dict:
    secret = get_settings().stripe_webhook_secret
    if not secret:
        raise ApiError(503, "webhook_not_configured", "STRIPE_WEBHOOK_SECRET (whsec_...) is not set.")

    # 1. Verify. Nothing is read or written before this passes.
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except stripe.SignatureVerificationError:
        raise ApiError(400, "invalid_signature", "Webhook signature verification failed.")
    except ValueError:
        raise ApiError(400, "invalid_payload", "Webhook body is not valid JSON.")

    event_id, etype = event["id"], event["type"]
    created = int(event["created"])
    obj = event["data"]["object"].to_dict() if hasattr(event["data"]["object"], "to_dict") \
        else dict(event["data"]["object"])

    # 2. Deduplicate. The marker row commits with the state change, or not at all.
    if db.get(StripeEvent, event_id) is not None:
        return {"status": "duplicate_ignored", "event_id": event_id, "type": etype}
    db.add(StripeEvent(event_id=event_id, type=etype, received_at=clock.now()))

    try:
        handler = HANDLERS.get(etype)
        result = handler(db, obj, created) if handler else "ignored_event_type"
        db.commit()
    except IntegrityError:
        db.rollback()  # a concurrent delivery of this same event committed first
        return {"status": "duplicate_ignored", "event_id": event_id, "type": etype}
    log.info("stripe event %s (%s): %s", event_id, etype, result)
    return {"status": "processed", "event_id": event_id, "type": etype, "result": result}
