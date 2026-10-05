"""HTTP layer: routes, auth, status codes. No SQL and no business rules live here —
routes call services and translate their results into responses."""

from __future__ import annotations

import logging
import math
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import billing, clock, jobs, metering
from .auth import current_tenant, hash_key, new_api_key, require_admin
from .config import get_settings
from .db import get_db
from .errors import ApiError
from .models import Notification, Plan, Tenant
from .pricing import TokenUsage
from .schemas import GenerateIn, TenantIn

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("billing.api")

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,255}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    worker = None
    if settings.run_worker:
        worker = jobs.Worker(settings.worker_poll_seconds)
        worker.start()
        log.info("background worker started (poll every %ss)", settings.worker_poll_seconds)
    yield
    if worker:
        worker.stop()


app = FastAPI(title="Usage Metering & Billing Engine", version="1.0.0", lifespan=lifespan)


# --- error handling: one shape, never a raw 500 for bad input -----------------

@app.exception_handler(ApiError)
async def api_error(_: Request, exc: ApiError):
    return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    problems = [{"field": ".".join(str(p) for p in e.get("loc", ()) if p != "body"),
                 "problem": e.get("msg")} for e in exc.errors()]
    return JSONResponse({"error": {"code": "invalid_request",
                                   "message": "The request body failed validation.",
                                   "problems": problems}}, status_code=422)


@app.exception_handler(Exception)
async def unexpected(_: Request, exc: Exception):
    log.exception("unhandled error")  # stack trace to logs; nothing internal to the client
    return JSONResponse({"error": {"code": "internal_error",
                                   "message": "Something went wrong on our side."}}, status_code=500)


# --- public -----------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/plans")
def plans(db: Session = Depends(get_db)):
    rows = db.execute(select(Plan).order_by(Plan.monthly_fee_cents)).scalars().all()
    return [{"code": p.code, "name": p.name, "api_calls_limit": p.api_calls_limit,
             "ai_tokens_limit": p.ai_tokens_limit, "monthly_fee_cents": p.monthly_fee_cents}
            for p in rows]


# --- admin ------------------------------------------------------------------

@app.post("/tenants", status_code=201, dependencies=[Depends(require_admin)])
def create_tenant(body: TenantIn, db: Session = Depends(get_db)):
    api_key = new_api_key()
    tenant = Tenant(id=str(uuid.uuid4()), name=body.name, api_key_hash=hash_key(api_key),
                    plan_code="free", subscription_status="none", stripe_state_at=0,
                    created_at=clock.now())
    db.add(tenant)
    db.commit()
    return {"tenant_id": tenant.id, "name": tenant.name, "plan": "free",
            "api_key": api_key, "note": "Store this key now — it is not shown again."}


# --- tenant -----------------------------------------------------------------

def _default_usage(prompt: str) -> TokenUsage:
    """Deterministic simulated token counts when the caller doesn't supply them."""
    return TokenUsage(input_tokens=max(1, math.ceil(len(prompt) / 4)), cached_input_tokens=0,
                      output_tokens=200, reasoning_tokens=0)


@app.post("/generate", status_code=201)
def generate(
    body: GenerateIn,
    tenant: Tenant = Depends(current_tenant),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """The billable action: meter -> check quota -> price -> respond. AI is simulated;
    we meter numbers, not model calls."""
    if not idempotency_key:
        raise ApiError(400, "missing_idempotency_key",
                       "Billable requests need an Idempotency-Key header so a retry is never "
                       "charged twice.")
    if not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise ApiError(400, "invalid_idempotency_key",
                       "Idempotency-Key must be 1-255 characters of letters, digits, _ - : .")

    usage = (TokenUsage(**body.usage.model_dump()) if body.usage else _default_usage(body.prompt))
    completion = f"[simulated completion for a {len(body.prompt)}-character prompt]"
    status, payload, replayed = metering.record_billable(
        db, tenant.id, idempotency_key, body.model_dump(), usage, completion)
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(payload, status_code=status, headers=headers)


@app.get("/usage")
def usage(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)):
    return metering.usage_summary(db, tenant)


@app.get("/notifications")
def notifications(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Notification).where(Notification.tenant_id == tenant.id).order_by(Notification.id)
    ).scalars().all()
    return [{"metric": n.metric, "threshold": n.threshold, "period": n.period,
             "message": n.message, "created_at": clock.as_utc(n.created_at).isoformat()}
            for n in rows]


# --- Stripe (test mode) -------------------------------------------------------

@app.post("/billing/checkout")
def checkout(tenant: Tenant = Depends(current_tenant)):
    """Start the Free -> Pro upgrade. Returns a Stripe-hosted Checkout URL; the plan
    only changes when Stripe's signed webhook confirms the subscription."""
    return billing.create_checkout_session(tenant)


@app.get("/billing/success")
def checkout_success(session_id: str = ""):
    return {"status": "checkout_complete", "session_id": session_id,
            "note": "Your plan updates as soon as Stripe's webhook arrives — check GET /usage."}


@app.get("/billing/cancel")
def checkout_cancel():
    return {"status": "checkout_canceled", "note": "No charge was made. Your plan is unchanged."}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()  # RAW bytes: the signature covers the exact body
    sig = request.headers.get("Stripe-Signature")
    return await run_in_threadpool(billing.handle_webhook, db, payload, sig)
