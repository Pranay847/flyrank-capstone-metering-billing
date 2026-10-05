"""One place that knows the time, so tests can pin it (and a billing period) exactly."""

from __future__ import annotations

from datetime import datetime, timezone

_frozen: datetime | None = None


def now() -> datetime:
    return _frozen or datetime.now(timezone.utc)


def freeze(at: datetime | None) -> None:
    global _frozen
    _frozen = at


def as_utc(at: datetime) -> datetime:
    """Normalise a stored timestamp to UTC. SQLite hands back naive datetimes; they
    were written as UTC, so tag them rather than guessing a local zone."""
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)


def period_of(at: datetime) -> str:
    """Billing period key: calendar month in UTC, e.g. '2026-10'."""
    return at.strftime("%Y-%m")


def next_period_start(at: datetime) -> datetime:
    y, m = (at.year + 1, 1) if at.month == 12 else (at.year, at.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc)
