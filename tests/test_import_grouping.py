"""Тесты импорта истории.

Проверяется сборка потока исполнений в отдельные сделки — место, где
ошибка тихо исказит всю статистику: склеенные позиции дадут неверный
средний вход, а потерянная дедупликация задвоит результат.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.exchanges.base import Fill
from app.services.import_service import group_fills_into_trades
from app.trading.enums import TradeSide

D = Decimal
BASE = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def fill(
    fid: str,
    minutes: int,
    *,
    symbol: str = "BTC-USDT",
    side: TradeSide = TradeSide.LONG,
    entry: bool = True,
    price: str = "100000",
    qty: str = "0.1",
    fee: str = "4",
) -> Fill:
    return Fill(
        external_id=fid,
        symbol=symbol,
        side=side,
        is_entry=entry,
        price=D(price),
        quantity=D(qty),
        fee=D(fee),
        realized_pnl=D(0),
        executed_at=BASE + timedelta(minutes=minutes),
    )


class TestGrouping:
    def test_single_entry_and_exit(self) -> None:
        trades = group_fills_into_trades([
            fill("1", 0),
            fill("2", 30, entry=False, price="102000"),
        ])
        assert len(trades) == 1
        assert trades[0].is_closed
        assert len(trades[0].fills) == 2

    def test_two_sequential_trades_stay_separate(self) -> None:
        """Ключевой случай: один инструмент, два входа за день.

        Если не разделить их по моменту обнуления позиции, получится
        одна сделка со средней ценой входа между двумя разными сетапами.
        """
        trades = group_fills_into_trades([
            fill("1", 0, price="100000"),
            fill("2", 30, entry=False, price="102000"),
            fill("3", 60, price="103000"),
            fill("4", 90, entry=False, price="101000"),
        ])
        assert len(trades) == 2
        assert trades[0].fills[0].price == D("100000")
        assert trades[1].fills[0].price == D("103000")

    def test_partial_exits_stay_one_trade(self) -> None:
        """Частичная фиксация не закрывает сделку."""
        trades = group_fills_into_trades([
            fill("1", 0, qty="0.2"),
            fill("2", 20, entry=False, qty="0.1", price="101000"),
            fill("3", 40, entry=False, qty="0.1", price="102000"),
        ])
        assert len(trades) == 1
        assert len(trades[0].fills) == 3
        assert trades[0].is_closed

    def test_scaled_entries(self) -> None:
        trades = group_fills_into_trades([
            fill("1", 0, qty="0.1", price="100000"),
            fill("2", 10, qty="0.1", price="99000"),
            fill("3", 60, entry=False, qty="0.2", price="102000"),
        ])
        assert len(trades) == 1
        assert trades[0].open_quantity == D("0")

    def test_long_and_short_on_same_symbol_are_separate(self) -> None:
        """Режим хеджирования: лонг и шорт по инструменту одновременно."""
        trades = group_fills_into_trades([
            fill("1", 0, side=TradeSide.LONG),
            fill("2", 5, side=TradeSide.SHORT),
            fill("3", 30, side=TradeSide.LONG, entry=False),
            fill("4", 40, side=TradeSide.SHORT, entry=False),
        ])
        assert len(trades) == 2
        assert {t.side for t in trades} == {TradeSide.LONG, TradeSide.SHORT}

    def test_different_symbols_are_separate(self) -> None:
        trades = group_fills_into_trades([
            fill("1", 0, symbol="BTC-USDT"),
            fill("2", 5, symbol="ETH-USDT", price="3000"),
            fill("3", 30, symbol="BTC-USDT", entry=False),
        ])
        assert len(trades) == 2

    def test_open_position_included_as_open_trade(self) -> None:
        trades = group_fills_into_trades([fill("1", 0)])
        assert len(trades) == 1
        assert not trades[0].is_closed
        assert trades[0].open_quantity == D("0.1")

    def test_exit_without_entry_is_skipped(self) -> None:
        """Позиция открыта до начала импорта — цену входа взять неоткуда.

        Придумывать её нельзя: сделка с выдуманным входом исказит
        и PnL, и статистику сильнее, чем её отсутствие.
        """
        trades = group_fills_into_trades([
            fill("1", 0, entry=False, price="102000"),
            fill("2", 30, price="103000"),
        ])
        assert len(trades) == 1
        assert trades[0].fills[0].external_id == "2"

    def test_order_of_input_does_not_matter(self) -> None:
        """Биржа может отдать исполнения в произвольном порядке."""
        shuffled = [
            fill("3", 60, price="103000"),
            fill("1", 0, price="100000"),
            fill("4", 90, entry=False, price="101000"),
            fill("2", 30, entry=False, price="102000"),
        ]
        trades = group_fills_into_trades(shuffled)
        assert len(trades) == 2
        assert trades[0].fills[0].external_id == "1"

    def test_empty_input(self) -> None:
        assert group_fills_into_trades([]) == []
