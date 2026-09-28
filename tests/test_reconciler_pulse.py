"""Пульс reconciler (28.09): тишина reconciler была неотличима от его
остановки — цикл без действий на INFO не писал ничего.

Классы импортируются внутри тестов: на коде до 28.09 их нет, и падать должен
каждый тест, а не сбор файла.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

T0 = datetime(2026, 9, 28, 6, 38, tzinfo=UTC)


def _pulse(started_at: datetime = T0):  # type: ignore[no-untyped-def]
    from app.workers.reconciler import ReconcilerPulse

    return ReconcilerPulse(started_at)


def test_window_counts_last_day_only() -> None:
    pulse = _pulse(T0 - timedelta(days=3))
    pulse.record_cycle(T0 - timedelta(hours=25), errors=1)  # за окном
    pulse.record_cycle(T0 - timedelta(hours=2), errors=0)
    pulse.record_cycle(T0 - timedelta(minutes=1), errors=2)

    window = pulse.window(T0)
    assert window.cycles == 2
    assert window.errors == 2
    assert window.last_cycle_at == T0 - timedelta(minutes=1)
    assert window.since_start is False


def test_restart_inside_window_is_flagged() -> None:
    pulse = _pulse(T0 - timedelta(hours=3))
    pulse.record_cycle(T0, errors=0)
    assert pulse.window(T0).since_start is True


def test_log_once_per_n_runs_with_counters(caplog) -> None:  # type: ignore[no-untyped-def]
    pulse = _pulse(T0)
    with caplog.at_level(logging.INFO, logger="app.workers.reconciler"):
        pulse.record_skip()
        pulse.log_if_due(T0 + timedelta(minutes=1), every=3)
        pulse.record_cycle(T0 + timedelta(minutes=2), errors=1)
        pulse.log_if_due(T0 + timedelta(minutes=2), every=3)
        pulse.log_events += 2
        pulse.log_redelivered += 1
        pulse.record_cycle(T0 + timedelta(minutes=3), errors=0)
        pulse.log_if_due(T0 + timedelta(minutes=3), every=3)
        pulse.record_cycle(T0 + timedelta(minutes=4), errors=0)
        pulse.log_if_due(T0 + timedelta(minutes=4), every=3)  # новый отсчёт — рано

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Пульс")]
    assert lines == [
        "Пульс reconciler: циклов 2 за 3 мин, пропущено по локу 1, ошибок 1, "
        "событий 2, переотправлено 1, последний 06:41:00 UTC"
    ]


def test_digest_line_in_user_tz() -> None:
    from app.workers.execution_digest import render_reconciler_line
    from app.workers.reconciler import ReconcilerWindow

    window = ReconcilerWindow(
        started_at=T0 - timedelta(days=2), since_start=False, cycles=1440, errors=0,
        last_cycle_at=datetime(2026, 9, 28, 15, 59, tzinfo=UTC),
    )
    assert render_reconciler_line(window, 5) == "Сверка: циклов 1440, последний 20:59, ошибок 0"

    restarted = ReconcilerWindow(
        started_at=datetime(2026, 9, 28, 11, 38, tzinfo=UTC), since_start=True, cycles=261,
        errors=3, last_cycle_at=datetime(2026, 9, 28, 15, 59, tzinfo=UTC),
    )
    assert (
        render_reconciler_line(restarted, 5)
        == "Сверка (с 16:38): циклов 261, последний 20:59, ошибок 3"
    )
