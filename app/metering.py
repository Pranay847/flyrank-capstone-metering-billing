"""MeterService: record a billable action exactly once, enforce quota at the exact
boundary, and roll usage up into money.

The whole billable request is ONE transaction that starts by locking the tenant row.
That lock serialises every billable request for a tenant, which is what makes both
guarantees hold under concurrency:
  * the same Idempotency-Key can never produce two usage events, and
  * two requests racing at 999/1,000 can never both pass the quota check.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import clock, jobs
from .errors import ApiError
from .models import IdempotencyKey, Plan, Tenant, UsageEvent
from .pricing import (TokenUsage, api_call_cost_pico, format_usd, pico_to_cents,
                      pico_to_micro, token_cost_pico)

PAYMENT_BLOCKED = {"past_due", "unpaid", "incomplete"}
THRESHOLDS = (80, 100)


def request_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- read path ---------------------------------------------------------------

def period_totals(db: Session, tenant_id: str, period: str) -> dict:
    """Sum of usage for one tenant and one period. Always scoped by tenant_id."""
    rows = db.execute(
        select(
            UsageEvent.type,
            func.coalesce(func.sum(UsageEvent.quantity), 0),
            func.coalesce(func.sum(UsageEvent.cost_pico), 0),
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cached_input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(UsageEvent.reasoning_tokens), 0),
        )
        .where(UsageEvent.tenant_id == tenant_id, UsageEvent.period == period)
        .group_by(UsageEvent.type)
    ).all()
    t = {"api_calls": 0, "ai_tokens": 0, "api_calls_pico": 0, "ai_tokens_pico": 0,
         "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    for typ, qty, pico, inp, cached, out, reasoning in rows:
        if typ == "api_call":
            t["api_calls"], t["api_calls_pico"] = int(qty), int(pico)
        elif typ == "ai_tokens":
            t["ai_tokens"], t["ai_tokens_pico"] = int(qty), int(pico)
            t.update(input_tokens=int(inp), cached_input_tokens=int(cached),
                     output_tokens=int(out), reasoning_tokens=int(reasoning))
    return t


def _metric(used: int, limit: int) -> dict:
    return {"used": used, "limit": limit, "remaining": max(limit - used, 0),
            "percent": round(used * 100 / limit, 1) if limit else 0.0}


def usage_summary(db: Session, tenant: Tenant) -> dict:
    now = clock.now()
    period = clock.period_of(now)
    plan = db.get(Plan, tenant.plan_code)
    t = period_totals(db, tenant.id, period)
    total_pico = t["api_calls_pico"] + t["ai_tokens_pico"]  # exact integers, summed first
    return {
        "tenant_id": tenant.id,
        "plan": {"code": plan.code, "name": plan.name, "monthly_fee_cents": plan.monthly_fee_cents},
        "subscription_status": tenant.subscription_status,
        "period": period,
        "resets_at": clock.next_period_start(now).isoformat(),
        "api_calls": _metric(t["api_calls"], plan.api_calls_limit),
        "ai_tokens": {
            **_metric(t["ai_tokens"], plan.ai_tokens_limit),
            "breakdown": {k: t[k] for k in ("input_tokens", "cached_input_tokens",
                                            "output_tokens", "reasoning_tokens")},
        },
        "cost": {
            "api_calls_pico_usd": t["api_calls_pico"],
            "ai_tokens_pico_usd": t["ai_tokens_pico"],
            "total_pico_usd": total_pico,
            "total_micro_usd": pico_to_micro(total_pico),   # rounded once, here
            "total_cents": pico_to_cents(total_pico),
            "total_display": format_usd(total_pico),
        },
    }


# --- write path ---------------------------------------------------------------

def _quota_error(tenant: Tenant, plan: Plan, metric: str, used: int, requested: int,
                 limit: int, now: datetime) -> ApiError:
    resets_at = clock.next_period_start(now)
    if metric == "api_calls":
        label = "API call" if requested == 1 else "API calls"
    else:
        label = "AI token" if requested == 1 else "AI tokens"
    details = {"metric": metric, "used": used, "requested": requested, "limit": limit,
               "remaining": max(limit - used, 0), "plan": plan.code,
               "resets_at": resets_at.isoformat()}
    if plan.code == "free":
        return ApiError(
            402, "quota_exceeded_upgrade_required",
            f"This request needs {requested:,} {label} but your Free plan has "
            f"{max(limit - used, 0):,} of {limit:,} left this month. Upgrade to Pro "
            f"(POST /billing/checkout) to continue now, or wait for the reset.",
            {**details, "upgrade": "/billing/checkout"},
        )
    retry_after = max(int((resets_at - now).total_seconds()), 1)
    return ApiError(
        429, "quota_exceeded",
        f"This request needs {requested:,} {label} but your {plan.name} plan has "
        f"{max(limit - used, 0):,} of {limit:,} left this month. Usage resets at "
        f"{resets_at.isoformat()}.",
        {**details, "retry_after_seconds": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


def _enqueue_threshold_alerts(db: Session, tenant: Tenant, plan: Plan, period: str,
                              metric: str, before: int, after: int, limit: int) -> None:
    for t in THRESHOLDS:
        if before * 100 < t * limit <= after * 100:  # crossed on THIS request
            jobs.enqueue(db, "usage_threshold_alert", {
                "tenant_id": tenant.id, "metric": metric, "threshold": t, "period": period,
                "used": after, "limit": limit, "plan_name": plan.name,
            })


def record_billable(db: Session, tenant_id: str, idempotency_key: str, body: dict,
                    usage: TokenUsage, completion: str) -> tuple[int, dict, bool]:
    """Returns (status_code, response_body, replayed)."""
    req_hash = request_hash(body)
    try:
        # 1. Serialise all billable work for this tenant.
        tenant = db.execute(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        ).scalar_one()

        # 2. Idempotency: seen this key before?
        prior = db.execute(
            select(IdempotencyKey).where(IdempotencyKey.tenant_id == tenant.id,
                                         IdempotencyKey.key == idempotency_key)
        ).scalar_one_or_none()
        if prior is not None:
            if prior.request_hash != req_hash:
                raise ApiError(409, "idempotency_key_reused",
                               "This Idempotency-Key was already used with a different request "
                               "body. Use a new key for a new request.")
            db.rollback()
            return prior.response_code, json.loads(prior.response_body), True

        # 3. Payment standing, then quota — both before anything is recorded.
        plan = db.get(Plan, tenant.plan_code)
        if tenant.plan_code != "free" and tenant.subscription_status in PAYMENT_BLOCKED:
            raise ApiError(402, "payment_required",
                           f"Your Pro subscription is {tenant.subscription_status}. Update your "
                           f"payment method in Stripe to resume billable requests.",
                           {"subscription_status": tenant.subscription_status})

        now = clock.now()
        period = clock.period_of(now)
        totals = period_totals(db, tenant.id, period)
        asks = {"api_calls": (1, plan.api_calls_limit),
                "ai_tokens": (usage.quota_tokens, plan.ai_tokens_limit)}
        for metric, (requested, limit) in asks.items():
            if totals[metric] + requested > limit:  # allowed iff used + requested <= limit
                raise _quota_error(tenant, plan, metric, totals[metric], requested, limit, now)

        # 4. Record exactly one event per type, priced exactly.
        call_pico = api_call_cost_pico(1)
        token_pico = token_cost_pico(usage)
        call_ev = UsageEvent(tenant_id=tenant.id, type="api_call", quantity=1, cost_pico=call_pico,
                             idempotency_key=idempotency_key, period=period, created_at=now)
        token_ev = UsageEvent(tenant_id=tenant.id, type="ai_tokens", quantity=usage.quota_tokens,
                              input_tokens=usage.input_tokens,
                              cached_input_tokens=usage.cached_input_tokens,
                              output_tokens=usage.output_tokens,
                              reasoning_tokens=usage.reasoning_tokens,
                              cost_pico=token_pico, idempotency_key=idempotency_key,
                              period=period, created_at=now)
        db.add_all([call_ev, token_ev])
        db.flush()

        for metric, (requested, limit) in asks.items():
            _enqueue_threshold_alerts(db, tenant, plan, period, metric,
                                      totals[metric], totals[metric] + requested, limit)

        total_pico = call_pico + token_pico
        response = {
            "idempotency_key": idempotency_key,
            "completion": completion,
            "metered": {
                "api_calls": 1,
                "ai_tokens": usage.quota_tokens,
                "token_breakdown": {
                    "input_tokens": usage.input_tokens,
                    "cached_input_tokens": usage.cached_input_tokens,
                    "fresh_input_tokens": usage.fresh_input_tokens,
                    "output_tokens": usage.output_tokens,
                    "reasoning_tokens": usage.reasoning_tokens,
                },
            },
            "cost": {"api_call_pico_usd": call_pico, "ai_tokens_pico_usd": token_pico,
                     "total_pico_usd": total_pico, "total_display": format_usd(total_pico)},
            "usage_after": {
                "period": period,
                "api_calls": _metric(totals["api_calls"] + 1, plan.api_calls_limit),
                "ai_tokens": _metric(totals["ai_tokens"] + usage.quota_tokens, plan.ai_tokens_limit),
            },
            "event_ids": [call_ev.id, token_ev.id],
            "recorded_at": now.isoformat(),
        }

        # 5. Store the response under the key, in the SAME transaction as the events.
        db.add(IdempotencyKey(tenant_id=tenant.id, key=idempotency_key, request_hash=req_hash,
                              response_code=201, response_body=json.dumps(response),
                              created_at=now))
        db.commit()
        return 201, response, False

    except ApiError:
        db.rollback()  # rejections record nothing, so the key stays reusable
        raise
    except IntegrityError:
        # Defence in depth: a concurrent writer committed this key first. Replay theirs.
        db.rollback()
        prior = db.execute(
            select(IdempotencyKey).where(IdempotencyKey.tenant_id == tenant_id,
                                         IdempotencyKey.key == idempotency_key)
        ).scalar_one_or_none()
        if prior is None:
            raise
        if prior.request_hash != req_hash:
            raise ApiError(409, "idempotency_key_reused",
                           "This Idempotency-Key was already used with a different request body.")
        return prior.response_code, json.loads(prior.response_body), True
