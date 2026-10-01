"""Тесты app/workers/execution_digest.py — сводка сверки с биржей.

Чистая логика подсчёта и рендера — без БД и без сети: build_stats() читает
готовые ReconciliationEvent, собранные тут вручную, без сессии. Воронка
входа по сигналу удалена вместе с ним (02.10.2026).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.workers.execution_digest import (
    build_stats,
    detect_anomalies,
    render_execution_digest,
)
from app.workers.reconciler import ReconcilerPulse


class TestRenderEmptyDay:
    def test_empty_day_renders_without_errors(self) -> None:
        """Раздел 12а: нулевой день — тоже сводка, а не молчание."""
        text = render_execution_digest(build_stats([]))
        lines = text.splitlines()
        assert lines[0] == "📊 <b>Исполнение за последние 24 часа</b>"
        assert "Сверка с биржей: событий нет" in lines
        assert "Аномалии: нет" in lines

    def test_no_entry_funnel_left(self) -> None:
        """Вход по сигналу удалён — ни карточек, ни READY в сводке."""
        text = render_execution_digest(build_stats([]))
        for gone in ("READY", "карточк", "Средний", "Скан рынка"):
            assert gone not in text

    def test_pulse_line_is_last(self) -> None:
        pulse = ReconcilerPulse(datetime.now(UTC) - timedelta(days=2))
        pulse.record_cycle(datetime.now(UTC) - timedelta(minutes=1), errors=0)
        text = render_execution_digest(
            build_stats([]), reconciler=pulse.window(datetime.now(UTC))
        )
        last = text.splitlines()[-1]
        assert last.startswith("Сверка: циклов 1, последний ")
        assert last.endswith("ошибок 0")


class TestReconcilerInDigest:
    """Шаг 15.6: расхождения reconciler — «Аномалии», факты — строка сводки."""

    @staticmethod
    def _event(kind, *, delivered: bool = True, symbol: str = "LINK-USDT"):  # type: ignore[no-untyped-def]
        # 28.09: недоставленное — отдельная аномалия; по умолчанию доставлено.
        from datetime import UTC, datetime

        from app.database.models.reconciliation_event import ReconciliationEvent

        return ReconciliationEvent(
            user_id=1, symbol=symbol, kind=kind, dedup_key=f"k:{kind}", detail="d",
            notified_at=datetime.now(UTC) if delivered else None,
        )

    def test_undelivered_notifications_are_an_anomaly(self) -> None:
        """28.09: уведомление сверки так и не ушло (ещё в переотправке или
        отказ) — строка в «Аномалиях» с последним по виду и символу."""
        from app.trading.enums import ReconciliationKind as K

        stats = build_stats([
            self._event(K.CLOSED_TAKE_PROFIT),
            self._event(K.STOP_MISSING, delivered=False, symbol="LINK-USDT"),
            self._event(K.CLOSED_STOP_LOSS, delivered=False, symbol="SOL-USDT"),
        ])
        anomalies = detect_anomalies(stats)
        assert (
            "уведомления сверки не доставлены — 2 (последнее: закрыто по стопу, SOL-USDT)"
            in anomalies
        )

    def test_discrepancies_are_anomalies(self) -> None:
        from app.trading.enums import ReconciliationKind as K

        stats = build_stats([
            self._event(K.ORPHAN_POSITION), self._event(K.STOP_MISSING),
            self._event(K.CLOSED_STOP_LOSS),
        ])
        anomalies = detect_anomalies(stats)
        assert (
            "сверка с биржей: расхождений 2 (позиция без сделки — 1, позиция без стопа — 1)"
            in anomalies
        )

    def test_closures_are_a_digest_line_not_anomaly(self) -> None:
        from app.trading.enums import ReconciliationKind as K

        stats = build_stats([self._event(K.CLOSED_STOP_LOSS), self._event(K.CLOSED_TAKE_PROFIT)])
        assert detect_anomalies(stats) == []
        text = render_execution_digest(stats)
        assert "Сверка с биржей: закрыто по стопу — 1, по тейку — 1" in text
