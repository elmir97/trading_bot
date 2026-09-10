"""Тесты статистики.

Проверяем на наборах с заранее посчитанным вручную результатом, включая
вырожденные случаи: ноль сделок, только победы, только убытки.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.trading.statistics import (
    TradeSnapshot,
    calculate_drawdown,
    calculate_statistics,
    calculate_streaks,
    group_by,
    group_by_hour,
    group_by_weekday,
)

D = Decimal
BASE = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def snap(
    pnl: str,
    offset_hours: int = 0,
    *,
    symbol: str = "BTC-USDT",
    side: str = "LONG",
    strategy: str | None = None,
    timeframe: str | None = None,
    rr: str | None = None,
) -> TradeSnapshot:
    return TradeSnapshot(
        pnl=D(pnl),
        closed_at=BASE + timedelta(hours=offset_hours),
        symbol=symbol,
        side=side,
        strategy=strategy,
        timeframe=timeframe,
        risk_reward=D(rr) if rr is not None else None,
    )


class TestEmptyAndDegenerate:
    def test_no_trades(self) -> None:
        stats = calculate_statistics([])
        assert stats.total_trades == 0
        assert stats.win_rate == D("0")
        assert stats.profit_factor is None
        assert stats.drawdown.max_drawdown == D("0")

    def test_only_wins_has_no_profit_factor(self) -> None:
        """Без убытков profit factor не определён — не бесконечность."""
        stats = calculate_statistics([snap("100"), snap("50", 1)])
        assert stats.profit_factor is None
        assert stats.win_rate == D("100.00")
        assert stats.average_loss == D("0")

    def test_only_losses(self) -> None:
        stats = calculate_statistics([snap("-100"), snap("-50", 1)])
        assert stats.win_rate == D("0.00")
        assert stats.profit_factor == D("0.000")
        assert stats.average_loss == D("75.00")
        assert stats.largest_loss == D("-100.00")

    def test_breakeven_counted_separately(self) -> None:
        stats = calculate_statistics([snap("100"), snap("0", 1), snap("-50", 2)])
        assert stats.wins == 1
        assert stats.losses == 1
        assert stats.breakeven == 1


class TestCoreMetrics:
    def test_known_dataset(self) -> None:
        """Ручной расчёт: +100, -50, +200, -100, +50.

        Побед 3 из 5 = 60%. Валовая прибыль 350, валовой убыток 150.
        Profit factor = 350/150 = 2.333. Итог +200, среднее +40.
        """
        trades = [
            snap("100", 0), snap("-50", 1), snap("200", 2),
            snap("-100", 3), snap("50", 4),
        ]
        stats = calculate_statistics(trades)

        assert stats.total_trades == 5
        assert stats.wins == 3
        assert stats.losses == 2
        assert stats.win_rate == D("60.00")
        assert stats.total_pnl == D("200.00")
        assert stats.average_pnl == D("40.00")
        assert stats.average_win == D("116.67")
        assert stats.average_loss == D("75.00")
        assert stats.profit_factor == D("2.333")
        assert stats.largest_win == D("200.00")
        assert stats.largest_loss == D("-100.00")

    def test_expectancy_equals_average_pnl(self) -> None:
        """Expectancy через win rate должна совпасть со средним PnL."""
        trades = [snap("100", 0), snap("-50", 1), snap("200", 2), snap("-100", 3)]
        stats = calculate_statistics(trades)
        assert stats.expectancy == stats.average_pnl

    def test_negative_expectancy(self) -> None:
        """Высокий win rate не спасает при больших убытках.

        4 победы по 10 и 1 убыток на 100: win rate 80%, но матожидание
        отрицательное. Именно этот случай статистика должна показывать.
        """
        trades = [snap("10", i) for i in range(4)] + [snap("-100", 5)]
        stats = calculate_statistics(trades)
        assert stats.win_rate == D("80.00")
        assert stats.expectancy < 0
        assert stats.total_pnl == D("-60.00")

    def test_average_rr_ignores_missing_values(self) -> None:
        trades = [snap("100", 0, rr="2"), snap("-50", 1, rr="-1"), snap("30", 2)]
        stats = calculate_statistics(trades)
        assert stats.average_rr == D("0.50")

    def test_average_rr_none_without_data(self) -> None:
        assert calculate_statistics([snap("100")]).average_rr is None


class TestStreaks:
    def test_win_and_loss_streaks(self) -> None:
        trades = [
            snap("10", 0), snap("10", 1), snap("10", 2),
            snap("-10", 3), snap("-10", 4),
            snap("10", 5),
        ]
        wins, losses = calculate_streaks(trades)
        assert wins == 3
        assert losses == 2

    def test_breakeven_does_not_break_streak(self) -> None:
        trades = [snap("10", 0), snap("0", 1), snap("10", 2)]
        wins, _ = calculate_streaks(trades)
        assert wins == 2

    def test_streaks_follow_chronological_order(self) -> None:
        """Порядок определяется closed_at, а не порядком в списке."""
        trades = [snap("-10", 5), snap("10", 0), snap("10", 1), snap("10", 2)]
        wins, losses = calculate_streaks(trades)
        assert wins == 3
        assert losses == 1


class TestDrawdown:
    def test_no_drawdown_on_rising_equity(self) -> None:
        result = calculate_drawdown([snap("100", 0), snap("50", 1)])
        assert result.max_drawdown == D("0.00")

    def test_peak_to_trough(self) -> None:
        """+100, +100 (пик 200), затем -50, -30 (дно 120). Просадка 80."""
        trades = [snap("100", 0), snap("100", 1), snap("-50", 2), snap("-30", 3)]
        result = calculate_drawdown(trades)
        assert result.max_drawdown == D("80.00")
        assert result.peak_equity == D("200.00")
        assert result.trough_equity == D("120.00")
        assert result.max_drawdown_percent == D("40.00")

    def test_recovery_then_deeper_drawdown(self) -> None:
        """Вторая просадка глубже первой — должна быть выбрана она.

        Кривая эквити: 0 → 100 → 70 → 170 → 80.
        Первая просадка 30 (со 100 до 70), вторая 90 (со 170 до 80).
        """
        trades = [
            snap("100", 0), snap("-30", 1),
            snap("100", 2), snap("-90", 3),
        ]
        result = calculate_drawdown(trades)
        assert result.max_drawdown == D("90.00")
        assert result.peak_equity == D("170.00")
        assert result.trough_equity == D("80.00")

    def test_current_drawdown_tracks_last_state(self) -> None:
        trades = [snap("100", 0), snap("-40", 1)]
        result = calculate_drawdown(trades)
        assert result.current_drawdown == D("40.00")

    def test_percent_measured_from_peak_not_start(self) -> None:
        """Просадка 100 после роста до 1000 — это 10%, а не 100% от старта."""
        trades = [snap("1000", 0), snap("-100", 1)]
        result = calculate_drawdown(trades)
        assert result.max_drawdown_percent == D("10.00")

    def test_starting_equity_changes_percent(self) -> None:
        trades = [snap("-500", 0)]
        result = calculate_drawdown(trades, starting_equity=D("10000"))
        assert result.max_drawdown == D("500.00")
        assert result.max_drawdown_percent == D("5.00")


class TestGrouping:
    def test_group_by_symbol_sorted_by_pnl(self) -> None:
        trades = [
            snap("100", 0, symbol="BTC-USDT"),
            snap("-50", 1, symbol="ETH-USDT"),
            snap("200", 2, symbol="BTC-USDT"),
        ]
        groups = group_by(trades, "symbol")
        assert groups[0].key == "BTC-USDT"
        assert groups[0].trades == 2
        assert groups[0].total_pnl == D("300.00")
        assert groups[0].win_rate == D("100.00")
        assert groups[-1].key == "ETH-USDT"

    def test_group_by_side(self) -> None:
        trades = [
            snap("100", 0, side="LONG"),
            snap("-80", 1, side="SHORT"),
            snap("-20", 2, side="SHORT"),
        ]
        groups = {g.key: g for g in group_by(trades, "side")}
        assert groups["SHORT"].total_pnl == D("-100.00")
        assert groups["SHORT"].win_rate == D("0.00")

    def test_missing_strategy_labelled(self) -> None:
        groups = group_by([snap("100")], "strategy")
        assert groups[0].key == "не указано"

    def test_min_trades_filter(self) -> None:
        """Срез по одной сделке статистически бессмыслен — можно отсечь."""
        trades = [
            snap("100", 0, symbol="BTC-USDT"),
            snap("50", 1, symbol="BTC-USDT"),
            snap("10", 2, symbol="XRP-USDT"),
        ]
        groups = group_by(trades, "symbol", min_trades=2)
        assert [g.key for g in groups] == ["BTC-USDT"]

    def test_group_by_hour_uses_local_time(self) -> None:
        """12:00 UTC при сдвиге +5 — это 17:00 по Перми."""
        groups = group_by_hour([snap("100", 0)], tz_offset_hours=5)
        assert groups[0].key == "17:00"

    def test_group_by_weekday(self) -> None:
        # 2 марта 2026 — понедельник, 12:00 UTC = 17:00 местного
        groups = group_by_weekday([snap("100", 0)], tz_offset_hours=5)
        assert groups[0].key == "Понедельник"
