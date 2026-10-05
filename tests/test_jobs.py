"""Background job: usage alerts via a transactional outbox, retries, failure alert."""

import json
import logging
from datetime import timedelta

from sqlalchemy import select

from app import clock, jobs
from app.db import session
from app.models import Alert, Job, Notification
from helpers import gen, make_tenant, preload


def all_jobs():
    with session() as db:
        return db.execute(select(Job).order_by(Job.id)).scalars().all()


def test_crossing_80_percent_enqueues_one_alert_job(client):
    tid, key = make_tenant(client)
    preload(tid, "api_call", 799)
    assert gen(client, key, "cross-80").status_code == 201        # 799 -> 800 = 80%
    js = all_jobs()
    assert len(js) == 1 and js[0].type == "usage_threshold_alert" and js[0].status == "pending"

    assert jobs.run_once() == 1
    assert all_jobs()[0].status == "done"
    notes = client.get("/notifications", headers={"X-API-Key": key}).json()
    assert len(notes) == 1
    assert notes[0]["threshold"] == 80 and notes[0]["metric"] == "api_calls"
    assert "80%" in notes[0]["message"] and "800 of 1,000" in notes[0]["message"]


def test_one_request_crossing_both_thresholds_enqueues_two(client):
    tid, key = make_tenant(client)
    big = {"prompt": "x", "usage": {"input_tokens": 100_000, "output_tokens": 0}}
    assert gen(client, key, "big", big).status_code == 201         # 0 -> 100% of tokens
    payloads = [json.loads(j.payload) for j in all_jobs()]
    thresholds = sorted(p["threshold"] for p in payloads if p["metric"] == "ai_tokens")
    assert thresholds == [80, 100]


def test_not_crossing_does_not_enqueue(client):
    tid, key = make_tenant(client)
    preload(tid, "api_call", 100)
    gen(client, key, "quiet")
    assert all_jobs() == []


def test_a_rejected_request_enqueues_nothing(client):
    tid, key = make_tenant(client)
    preload(tid, "api_call", 1_000)
    assert gen(client, key, "rejected").status_code == 402
    assert all_jobs() == []          # outbox: no committed write, no job


def test_alert_is_sent_only_once_even_if_the_job_runs_twice(client):
    tid, key = make_tenant(client)
    payload = {"tenant_id": tid, "metric": "api_calls", "threshold": 80, "period": "2026-10",
               "used": 800, "limit": 1000, "plan_name": "Free"}
    with session() as db:
        jobs.enqueue(db, "usage_threshold_alert", payload)
        jobs.enqueue(db, "usage_threshold_alert", payload)
        db.commit()
    assert jobs.run_once() == 2
    with session() as db:
        assert len(db.execute(select(Notification)).scalars().all()) == 1
    assert all(j.status == "done" for j in all_jobs())


def test_failing_job_retries_with_backoff_then_raises_an_alert(client, caplog):
    calls = []

    @jobs.register("always_fails")
    def boom(db, payload):
        calls.append(1)
        raise RuntimeError("downstream mail provider unreachable")

    with session() as db:
        jobs.enqueue(db, "always_fails", {"x": 1}, max_attempts=3)
        db.commit()

    jobs.run_once()                                       # attempt 1
    j = all_jobs()[0]
    assert (j.status, j.attempts) == ("pending", 1)
    assert jobs.run_once() == 0                           # backoff: not due yet

    clock.freeze(clock.now() + timedelta(seconds=3))
    jobs.run_once()                                       # attempt 2
    assert all_jobs()[0].attempts == 2

    clock.freeze(clock.now() + timedelta(seconds=5))
    with caplog.at_level(logging.ERROR, logger="billing.jobs"):
        jobs.run_once()                                   # attempt 3 -> failed
    j = all_jobs()[0]
    assert (j.status, j.attempts, len(calls)) == ("failed", 3, 3)
    assert "unreachable" in j.last_error
    with session() as db:
        alerts = db.execute(select(Alert)).scalars().all()
    assert len(alerts) == 1 and "failed permanently after 3 attempts" in alerts[0].message
    assert any("failed permanently" in r.message for r in caplog.records)
