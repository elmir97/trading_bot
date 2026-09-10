"""Тесты app/workers/execution_digest.py (этап 15.4, раздел 12а ТЗ).

Чистая логика подсчёта и рендера — без БД и без сети: build_stats() и
detect_anomalies() читают уже готовые ExecutionOrder (role=ENTRY, как их
отдаёт ExecutionOrderRepository.list_entries_between()), собранные тут
вручную, без сессии. Как test_workers.py — те же принципы (Decimal,
никакого мока сети/БД).
"""

from __future__ import annotations

from decimal import Decimal

from app.database.models.execution_order import ExecutionOrder
from app.trading.enums import OrderRole, OrderSide, OrderStatus, OrderType, TradeSide
from app.workers.execution_digest import (
    build_stats,
    detect_anomalies,
    render_execution_digest,
)

D = Decimal


def _row(status: OrderStatus, **overrides: object) -> ExecutionOrder:
    fields: dict[str, object] = {
        "user_id": 1,
        "symbol": "BTC-USDT",
        "side": OrderSide.BUY,
        "position_side": TradeSide.LONG,
        "order_type": OrderType.MARKET,
        "role": OrderRole.ENTRY,
        "status": status,
    }
    fields.update(overrides)
    return ExecutionOrder(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Подсчёты по подтверждениям и отказам
# ---------------------------------------------------------------------------


class TestBuildStats:
    def test_counts_each_card_outcome_once(self) -> None:
        rows = [
            _row(OrderStatus.DRY_RUN, risk_percent=D("1.0"), risk_reward=D("2.5")),
            _row(OrderStatus.DRY_RUN, risk_percent=D("1.0"), risk_reward=D("2.0")),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.EXPIRED),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.confirmed == 2
        assert stats.declined == 3
        assert stats.expired == 1
        assert stats.total_cards == 6

    def test_refusals_grouped_by_code_not_counted_in_cards(self) -> None:
        rows = [
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="PRICE_DRIFT"),
            _row(OrderStatus.DRY_RUN, risk_percent=D("1.0")),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.refusals_by_code == {"MAX_POSITIONS": 2, "PRICE_DRIFT": 1}
        assert stats.total_refusals == 3
        assert stats.total_cards == 1  # отказы гварда не показывают карточку
        assert stats.total_attempts == 4

    def test_only_confirmed_rows_feed_averages(self) -> None:
        rows = [
            _row(
                OrderStatus.DRY_RUN, risk_percent=D("1.0"), risk_reward=D("2.0"),
                price_drift_percent=D("0.1"),
            ),
            # DECLINED/REFUSED со своими цифрами не должны попасть в средние.
            _row(OrderStatus.DECLINED, risk_percent=D("9.0"), risk_reward=D("9.0")),
            _row(OrderStatus.REFUSED, error_code="X"),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.confirmed_risk_percents == [D("1.0")]
        assert stats.confirmed_risk_rewards == [D("2.0")]
        assert stats.confirmed_drift_percents == [D("0.1")]


# ---------------------------------------------------------------------------
# Аномалии
# ---------------------------------------------------------------------------


class TestAnomalies:
    def test_no_anomalies_on_clean_day(self) -> None:
        rows = [
            _row(
                OrderStatus.DRY_RUN, symbol="BTC-USDT", risk_percent=D("1.95"),
                risk_reward=D("2.2"), price_drift_percent=D("0.12"),
            ),
        ]
        stats = build_stats(rows, target_risk_percent=D("2.0"))
        assert detect_anomalies(stats, max_price_drift_ratio=D("0.3")) == []

    def test_risk_deviation_over_10_percent_flagged(self) -> None:
        # Цель 2.0%, факт 1.7% — отклонение 15% > 10%, но < 50% (не undersized).
        rows = [_row(OrderStatus.DRY_RUN, symbol="ETH-USDT", risk_percent=D("1.7"))]
        stats = build_stats(rows, target_risk_percent=D("2.0"))
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert len(anomalies) == 1
        assert "ETH-USDT" in anomalies[0]
        assert stats.undersized == []

    def test_risk_deviation_within_10_percent_not_flagged(self) -> None:
        # 1.95% при цели 2.0% — отклонение 2.5%, в пределах допустимого.
        rows = [_row(OrderStatus.DRY_RUN, risk_percent=D("1.95"))]
        stats = build_stats(rows, target_risk_percent=D("2.0"))
        assert detect_anomalies(stats, max_price_drift_ratio=D("0.3")) == []

    def test_undersized_lot_flagged_separately_from_deviation(self) -> None:
        # 0.9% при цели 2.0% — отклонение 55% > 50%: и "отклонился", и
        # отдельно "слишком мелкий лот" — два разных пункта из раздела 12а.
        rows = [_row(OrderStatus.DRY_RUN, symbol="BTC-USDT", risk_percent=D("0.9"))]
        stats = build_stats(rows, target_risk_percent=D("2.0"))
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert len(anomalies) == 2
        assert any("меньше половины" in a for a in anomalies)

    def test_average_drift_above_half_threshold_flagged(self) -> None:
        # Допустимый порог 0.3 → в процентах то же самое (30%), половина —
        # 15%. Средний дрейф 20% > 15% — аномалия.
        rows = [
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("18")),
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("22")),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert any("дрейф" in a for a in anomalies)

    def test_average_drift_below_half_threshold_not_flagged(self) -> None:
        rows = [_row(OrderStatus.DRY_RUN, price_drift_percent=D("5"))]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert not any("дрейф" in a for a in anomalies)

    def test_dominant_guard_code_flagged(self) -> None:
        # 3 из 5 попыток отказаны одним и тем же гвардом — больше половины.
        rows = [
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.DRY_RUN, risk_percent=D("1")),
            _row(OrderStatus.DECLINED),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert any("MAX_POSITIONS" in a for a in anomalies)

    def test_evenly_spread_guard_codes_not_flagged(self) -> None:
        rows = [
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="PRICE_DRIFT"),
            _row(OrderStatus.DRY_RUN, risk_percent=D("1")),
            _row(OrderStatus.DECLINED),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert not any("срабатывает подозрительно часто" in a for a in anomalies)


# ---------------------------------------------------------------------------
# Рендер, включая нулевой день
# ---------------------------------------------------------------------------


class TestRenderExecutionDigest:
    def test_zero_signals_day_renders_without_errors(self) -> None:
        stats = build_stats([], target_risk_percent=D("2.0"))
        text = render_execution_digest(stats, max_price_drift_ratio=D("0.3"))
        assert "Сигналов READY: 0" in text
        assert "подтверждено: 0" in text
        assert "отказ пользователя: 0" in text
        assert "истекло по TTL: 0" in text
        assert "Отказы кода: 0" in text
        assert "Аномалии: нет" in text
        # Средних строк для пустого дня быть не должно — делить не на что.
        assert "Средний" not in text

    def test_normal_day_renders_counts_and_averages(self) -> None:
        rows = [
            _row(
                OrderStatus.DRY_RUN, risk_percent=D("1.0"), risk_reward=D("2.0"),
                price_drift_percent=D("0.1"),
            ),
            _row(
                OrderStatus.DRY_RUN, risk_percent=D("0.8"), risk_reward=D("2.4"),
                price_drift_percent=D("0.14"),
            ),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.EXPIRED),
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="PRICE_DRIFT"),
        ]
        # target_risk_percent=None — эта проверка про форматирование счётчиков
        # и средних, не про раздел "Аномалии" (у него свой TestAnomalies).
        stats = build_stats(rows, target_risk_percent=None)
        text = render_execution_digest(stats, max_price_drift_ratio=D("0.3"))

        assert "Сигналов READY: 6" in text
        assert "подтверждено: 2" in text
        assert "отказ пользователя: 3" in text
        assert "истекло по TTL: 1" in text
        assert "Отказы кода: 2" in text
        assert "MAX_POSITIONS — 1" in text
        assert "PRICE_DRIFT — 1" in text
        assert "Средний расчётный риск" in text
        assert "Средний RR" in text
        assert "Средний дрейф цены на подтверждении" in text
        assert "Аномалии: нет" in text
