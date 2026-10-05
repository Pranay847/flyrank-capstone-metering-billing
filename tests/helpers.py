"""Small helpers shared by the tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from sqlalchemy import func, select

from app import clock
from app.db import engine, session
from app.models import Tenant, UsageEvent

ADMIN = {"X-Admin-Token": "test-admin"}
WEBHOOK_SECRET = "whsec_test_secret"
SMALL = {"prompt": "x", "usage": {"input_tokens": 1, "output_tokens": 0}}  # 1 token


def is_postgres() -> bool:
    return engine().dialect.name == "postgresql"


def make_tenant(client, name="Acme", plan="free", status="none") -> tuple[str, str]:
    r = client.post("/tenants", json={"name": name}, headers=ADMIN)
    assert r.status_code == 201, r.text
    tid, key = r.json()["tenant_id"], r.json()["api_key"]
    if plan != "free" or status != "none":
        set_plan(tid, plan, status)
    return tid, key


def set_plan(tid: str, plan: str, status: str) -> None:
    with session() as db:
        t = db.get(Tenant, tid)
        t.plan_code, t.subscription_status = plan, status
        db.commit()


def preload(tid: str, kind: str, qty: int, key: str = "preload") -> None:
    """Put a tenant at a known usage level without sending thousands of requests."""
    with session() as db:
        db.add(UsageEvent(tenant_id=tid, type=kind, quantity=qty, cost_pico=0,
                          idempotency_key=key, period=clock.period_of(clock.now()),
                          created_at=clock.now()))
        db.commit()


def event_count(tid: str, key: str | None = None) -> dict:
    with session() as db:
        q = select(UsageEvent.type, func.count()).where(UsageEvent.tenant_id == tid)
        if key:
            q = q.where(UsageEvent.idempotency_key == key)
        return dict(db.execute(q.group_by(UsageEvent.type)).all())


def tenant(tid: str) -> Tenant:
    with session() as db:
        return db.get(Tenant, tid)


def gen(client, key, idem, body=None):
    # `body or SMALL` would be a bug: an empty {} is falsy and would be swapped out.
    return client.post("/generate", json=SMALL if body is None else body,
                       headers={"X-API-Key": key, "Idempotency-Key": idem})


def signed(evt: dict, secret: str = WEBHOOK_SECRET, ts: int | None = None) -> tuple[str, str]:
    """Sign an event exactly the way Stripe does: HMAC-SHA256 over 't.payload'."""
    payload = json.dumps(evt)
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return payload, f"t={ts},v1={sig}"


def post_event(client, evt: dict, **kw):
    payload, header = signed(evt, **kw)
    return client.post("/webhooks/stripe", content=payload,
                       headers={"Stripe-Signature": header, "Content-Type": "application/json"})


def checkout_event(tid, eid="evt_checkout_1", created=None, payment_status="paid"):
    return {"id": eid, "object": "event", "type": "checkout.session.completed",
            "created": created or int(time.time()),
            "data": {"object": {"id": "cs_test_1", "object": "checkout.session",
                                "mode": "subscription", "payment_status": payment_status,
                                "client_reference_id": tid, "customer": "cus_test_1",
                                "subscription": "sub_test_1", "metadata": {"tenant_id": tid}}}}


def subscription_event(tid, etype, status, eid, created=None, sub_id="sub_test_1"):
    return {"id": eid, "object": "event", "type": etype, "created": created or int(time.time()),
            "data": {"object": {"id": sub_id, "object": "subscription", "status": status,
                                "customer": "cus_test_1", "metadata": {"tenant_id": tid},
                                "current_period_end": 1_793_000_000}}}
