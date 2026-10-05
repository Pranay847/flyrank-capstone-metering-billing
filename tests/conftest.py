"""Test setup. Runs against TEST_DATABASE_URL if set (use Postgres to exercise row
locks and the concurrency tests), otherwise a throwaway SQLite file. The schema is
built by the real Alembic migrations, not create_all()."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

os.environ.setdefault("ADMIN_TOKEN", "test-admin")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
os.environ["RUN_WORKER"] = "false"   # tests drive the worker deterministically via run_once()
os.environ.pop("STRIPE_SECRET_KEY", None)
os.environ.pop("STRIPE_PRICE_PRO", None)

_url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["DATABASE_URL"] = _url

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import clock  # noqa: E402
from app.db import engine, init_engine, migrate  # noqa: E402

init_engine(_url)
migrate()

TABLES = ["alerts", "notifications", "jobs", "stripe_events", "idempotency_keys",
          "usage_events", "subscriptions", "tenants"]
FROZEN = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_db():
    with engine().begin() as conn:
        for t in TABLES:
            conn.execute(text(f"DELETE FROM {t}"))
    clock.freeze(FROZEN)
    yield
    clock.freeze(None)


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def pytest_report_header(config):
    return f"database: {engine().dialect.name}"
