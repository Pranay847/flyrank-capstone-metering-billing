"""Settings, read from the environment. Secrets live in .env (git-ignored) and are
never logged. Nothing here has a real default secret."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_token: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_pro: str
    public_base_url: str
    run_worker: bool
    worker_poll_seconds: float


def get_settings() -> Settings:
    url = os.environ.get("DATABASE_URL", "sqlite:///./billing.db")
    if url.startswith("postgres://"):  # common shorthand
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return Settings(
        database_url=url,
        admin_token=os.environ.get("ADMIN_TOKEN", ""),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
        stripe_price_pro=os.environ.get("STRIPE_PRICE_PRO", ""),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"),
        run_worker=_bool("RUN_WORKER", True),
        worker_poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", "2")),
    )
