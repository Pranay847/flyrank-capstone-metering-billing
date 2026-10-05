"""Engine, sessions, and migrations. The schema is owned by Alembic migrations —
the app never calls create_all()."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None

ROOT = Path(__file__).resolve().parents[1]


def init_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def engine() -> Engine:
    return _engine or init_engine()


def session() -> Session:
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal()  # type: ignore[misc]


def get_db():
    """FastAPI dependency: one session per request, always closed."""
    s = session()
    try:
        yield s
    finally:
        s.close()


def migrate(url: str | None = None) -> None:
    """Apply every Alembic migration up to head."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url or engine().url.render_as_string(hide_password=False)
    command.upgrade(cfg, "head")
