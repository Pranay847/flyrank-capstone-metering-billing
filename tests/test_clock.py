"""Billing-period math in app.clock: month keys and resets are always UTC."""

from datetime import datetime, timedelta, timezone

from app import clock

UTC = timezone.utc
EST = timezone(timedelta(hours=-5))
IST = timezone(timedelta(hours=5, minutes=30))


def test_period_of_uses_utc_month_for_offset_inputs():
    assert clock.period_of(datetime(2026, 10, 31, 23, 59, tzinfo=UTC)) == "2026-10"
    # 23:30 at UTC-5 is already 04:30 Nov 1 in UTC.
    assert clock.period_of(datetime(2026, 10, 31, 23, 30, tzinfo=EST)) == "2026-11"
    # 02:00 Nov 1 at UTC+5:30 is still Oct 31 in UTC.
    assert clock.period_of(datetime(2026, 11, 1, 2, 0, tzinfo=IST)) == "2026-10"


def test_next_period_start_rolls_months_and_years_in_utc():
    assert clock.next_period_start(datetime(2026, 10, 15, tzinfo=UTC)) == datetime(2026, 11, 1, tzinfo=UTC)
    assert clock.next_period_start(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(2027, 1, 1, tzinfo=UTC)
    # Dec 31 19:30 at UTC-5 is Jan 1 UTC, so the next reset is Feb 1.
    assert clock.next_period_start(datetime(2026, 12, 31, 19, 30, tzinfo=EST)) == datetime(2027, 2, 1, tzinfo=UTC)


def test_as_utc_tags_naive_and_converts_aware():
    naive = datetime(2026, 10, 15, 12, 0)
    assert clock.as_utc(naive) == datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    assert clock.as_utc(datetime(2026, 10, 15, 7, 0, tzinfo=EST)).hour == 12
    assert clock.as_utc(datetime(2026, 10, 15, 7, 0, tzinfo=EST)).tzinfo is UTC


def test_freeze_pins_now_and_none_restores_wall_clock():
    pinned = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
    clock.freeze(pinned)
    assert clock.now() == pinned
    clock.freeze(None)
    live = clock.now()
    assert live.tzinfo is not None and abs(live - datetime.now(UTC)) < timedelta(seconds=5)
