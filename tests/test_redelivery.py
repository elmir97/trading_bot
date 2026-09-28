"""Расписание переотправки уведомлений сверки (28.09) — без I/O.

Модуль импортируется внутри тестов: на коде до 28.09 его нет, и падать
должен каждый тест, а не сбор файла.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
FAST = timedelta(minutes=30)
SLOW = timedelta(minutes=10)
MAX_AGE = timedelta(hours=24)


def _due(now: datetime, last: datetime | None):  # type: ignore[no-untyped-def]
    from app.execution.redelivery import redelivery_due

    return redelivery_due(
        created_at=T0, last_attempt_at=last, now=now, fast=FAST, slow_interval=SLOW,
        max_age=MAX_AGE,
    )


def test_fast_window_every_cycle() -> None:
    for minutes in (1, 2, 15, 30):
        now = T0 + timedelta(minutes=minutes)
        decision = _due(now, now - timedelta(minutes=1))
        assert decision.due and not decision.entering_slow and not decision.expired


def test_first_cycle_after_fast_window_attempts_and_flags_slow() -> None:
    decision = _due(T0 + timedelta(minutes=31), T0 + timedelta(minutes=30))
    assert decision.due
    assert decision.entering_slow


def test_slow_mode_waits_ten_minutes() -> None:
    last = T0 + timedelta(minutes=31)
    assert not _due(T0 + timedelta(minutes=35), last).due
    assert not _due(T0 + timedelta(minutes=40), last).entering_slow
    later = _due(T0 + timedelta(minutes=41), last)
    assert later.due and not later.entering_slow


def test_older_than_max_age_expires() -> None:
    decision = _due(T0 + MAX_AGE + timedelta(minutes=1), T0 + timedelta(hours=23))
    assert decision.expired
    assert not decision.due


def test_late_notice_only_after_two_minutes_in_user_tz() -> None:
    from app.execution.redelivery import late_notice

    assert late_notice(created_at=T0, now=T0 + timedelta(minutes=2), tz_offset_hours=5) is None
    assert (
        late_notice(created_at=T0, now=T0 + timedelta(minutes=3), tz_offset_hours=5)
        == "⏱ Событие от 15:00 (доставлено с опозданием)"
    )
