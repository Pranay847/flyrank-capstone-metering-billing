"""Real races against Postgres. Threads call the metering service directly, each with
its own connection, released together by a barrier — so the row lock is what has to
hold, not test-client serialisation. Skipped on SQLite (it serialises writers anyway,
so a race there proves nothing)."""

import threading

import pytest

from app import metering
from app.db import session
from app.errors import ApiError
from app.pricing import TokenUsage
from helpers import event_count, is_postgres, make_tenant, preload

pytestmark = pytest.mark.skipif(not is_postgres(), reason="needs Postgres row locks")

USAGE = TokenUsage(input_tokens=1, cached_input_tokens=0, output_tokens=0, reasoning_tokens=0)
BODY = {"prompt": "x", "usage": None}


def race(n, fn):
    barrier, results = threading.Barrier(n), []
    lock = threading.Lock()

    def run(i):
        barrier.wait()
        out = fn(i)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def call(tid, key):
    with session() as db:
        try:
            status, _, replayed = metering.record_billable(db, tid, key, BODY, USAGE, "sim")
            return ("replay" if replayed else "new", status)
        except ApiError as e:
            return ("rejected", e.status)


def test_concurrent_retries_of_one_key_record_one_event(client):
    tid, _ = make_tenant(client)
    results = race(20, lambda i: call(tid, "same-key"))
    assert [r for r in results if r[0] == "new"] == [("new", 201)]
    assert sum(1 for r in results if r[0] == "replay") == 19
    assert event_count(tid) == {"api_call": 1, "ai_tokens": 1}


def test_concurrent_requests_at_the_boundary_admit_exactly_one(client):
    tid, _ = make_tenant(client)
    preload(tid, "api_call", 999)
    results = race(20, lambda i: call(tid, f"key-{i}"))
    assert sorted(r[1] for r in results).count(201) == 1
    assert sum(1 for r in results if r == ("rejected", 402)) == 19
    with session() as db:
        assert metering.period_totals(db, tid, "2026-10")["api_calls"] == 1_000  # never 1,001
