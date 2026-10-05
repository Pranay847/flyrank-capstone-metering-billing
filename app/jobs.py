"""A small, durable background-job queue backed by the `jobs` table.

- enqueue() writes a job inside the caller's transaction (an outbox), so a job exists
  if and only if the write that caused it committed.
- run_once() claims due jobs (SKIP LOCKED on Postgres, so two workers never take the
  same job), runs the handler in its own transaction, and retries failures with
  exponential backoff.
- After max_attempts the job is marked failed, an `alerts` row is written and an
  ERROR is logged. That is the failure alert.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import clock
from .db import session as new_session
from .models import Alert, Job, Notification

log = logging.getLogger("billing.jobs")

Handler = Callable[[Session, dict], None]
HANDLERS: dict[str, Handler] = {}
STALE_RUNNING = timedelta(minutes=5)


def register(job_type: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn
    return deco


def enqueue(db: Session, job_type: str, payload: dict, max_attempts: int = 3) -> Job:
    now = clock.now()
    job = Job(type=job_type, payload=json.dumps(payload, sort_keys=True), status="pending",
              attempts=0, max_attempts=max_attempts, run_after=now, created_at=now, updated_at=now)
    db.add(job)
    return job


def _claim(db: Session) -> Job | None:
    now = clock.now()
    stmt = (
        select(Job)
        .where(or_(
            (Job.status == "pending") & (Job.run_after <= now),
            (Job.status == "running") & (Job.updated_at <= now - STALE_RUNNING),  # crashed worker
        ))
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.execute(stmt).scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.attempts += 1
    job.updated_at = now
    db.commit()
    return job


def run_once(max_jobs: int = 50) -> int:
    """Process up to max_jobs due jobs. Returns how many were attempted."""
    processed = 0
    for _ in range(max_jobs):
        with new_session() as db:
            job = _claim(db)
            if job is None:
                break
            job_id, job_type, payload = job.id, job.type, json.loads(job.payload)
        processed += 1
        handler = HANDLERS.get(job_type)
        try:
            if handler is None:
                raise RuntimeError(f"no handler registered for job type {job_type!r}")
            with new_session() as db:
                handler(db, payload)
                job = db.get(Job, job_id)
                job.status, job.last_error, job.updated_at = "done", None, clock.now()
                db.commit()
        except Exception as exc:  # noqa: BLE001 — a job must never crash the worker
            _record_failure(job_id, exc)
    return processed


def _record_failure(job_id: int, exc: Exception) -> None:
    with new_session() as db:
        job = db.get(Job, job_id)
        now = clock.now()
        job.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        job.updated_at = now
        if job.attempts < job.max_attempts:
            job.status = "pending"
            job.run_after = now + timedelta(seconds=2 ** job.attempts)  # 2s, 4s, 8s...
            log.warning("job %s (%s) failed attempt %s/%s, retrying: %s",
                        job.id, job.type, job.attempts, job.max_attempts, job.last_error)
        else:
            job.status = "failed"
            msg = (f"Job {job.id} ({job.type}) failed permanently after {job.attempts} "
                   f"attempts: {job.last_error}")
            db.add(Alert(source=f"job:{job.type}", message=msg, created_at=now))
            log.error(msg)
        db.commit()


class Worker(threading.Thread):
    """Polls the queue in the background, off the request path."""

    def __init__(self, poll_seconds: float = 2.0):
        super().__init__(name="billing-worker", daemon=True)
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()  # not `_stop`: Thread already uses that name

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                run_once()
            except Exception:  # noqa: BLE001
                log.exception("worker loop error")
            self._stop_event.wait(self.poll_seconds)

    def stop(self) -> None:
        self._stop_event.set()


# --- handlers ----------------------------------------------------------------

@register("usage_threshold_alert")
def usage_threshold_alert(db: Session, p: dict) -> None:
    """Tell a tenant they reached 80% / 100% of a quota. Idempotent: the unique
    constraint means a retried job never sends the same alert twice."""
    label = "API calls" if p["metric"] == "api_calls" else "AI tokens"
    message = (f"You have used {p['threshold']}% of your {p['plan_name']} plan's monthly "
               f"{label} ({p['used']:,} of {p['limit']:,}) for {p['period']}.")
    db.add(Notification(tenant_id=p["tenant_id"], metric=p["metric"], threshold=p["threshold"],
                        period=p["period"], message=message, created_at=clock.now()))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()  # already sent — retry is a no-op
        return
    log.info("notified tenant %s: %s", p["tenant_id"], message)
