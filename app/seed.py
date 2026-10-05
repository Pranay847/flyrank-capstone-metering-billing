"""Demo data: three tenants with known API keys, for local runs and evaluation only.

    python -m app.seed

Idempotent — safe to run any number of times. These keys are public demo values,
not secrets; real tenants are created with POST /tenants and get random keys.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from . import clock
from .auth import hash_key
from .db import init_engine, migrate, session
from .models import Tenant, UsageEvent

DEMO = [
    # name,              api key,               plan,   status,   preloaded API calls
    ("Demo Free",        "demo-free-key",       "free", "none",   0),
    ("Demo Pro",         "demo-pro-key",        "pro",  "active", 0),
    ("Demo Boundary",    "demo-boundary-key",   "free", "none",   998),
]


def main() -> None:
    init_engine()
    migrate()
    now = clock.now()
    with session() as db:
        for name, key, plan, status, preload in DEMO:
            t = db.execute(select(Tenant).where(Tenant.api_key_hash == hash_key(key))).scalar_one_or_none()
            if t is None:
                t = Tenant(id=str(uuid.uuid4()), name=name, api_key_hash=hash_key(key),
                           plan_code=plan, subscription_status=status, stripe_state_at=0,
                           created_at=now)
                db.add(t)
                db.flush()
            if preload:
                exists = db.execute(select(UsageEvent).where(
                    UsageEvent.tenant_id == t.id, UsageEvent.idempotency_key == "seed-preload",
                    UsageEvent.period == clock.period_of(now))).scalar_one_or_none()
                if exists is None:
                    db.add(UsageEvent(tenant_id=t.id, type="api_call", quantity=preload, cost_pico=0,
                                      idempotency_key="seed-preload", period=clock.period_of(now),
                                      created_at=now))
        db.commit()

    print("Seeded demo tenants (use as X-API-Key):\n")
    print(f"  {'tenant':<16} {'api key':<20} {'plan':<5} note")
    print(f"  {'Demo Free':<16} {'demo-free-key':<20} {'free':<5} empty")
    print(f"  {'Demo Pro':<16} {'demo-pro-key':<20} {'pro':<5} active subscription")
    print(f"  {'Demo Boundary':<16} {'demo-boundary-key':<20} {'free':<5} 998 / 1,000 calls used")


if __name__ == "__main__":
    main()
