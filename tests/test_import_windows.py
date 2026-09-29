"""Регрессия: разбиение периода на окна зацикливалось.

Следующее окно начиналось на OVERLAP раньше конца предыдущего. Когда
остаток периода оказывался меньше перекрытия, начало окна переставало
двигаться вперёд, и цикл крутился вечно — фоновая задача импорта
повисла бы навсегда, без ошибки в логе.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.exchanges.base import Fill
from app.services.import_service import OVERLAP, WINDOW, HistoryImporter
from app.trading.enums import TradeSide

BASE = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


class CountingExchange:
    """Считает окна и обрывается, если их стало подозрительно много."""

    name = "bingx"
    LIMIT = 400

    def __init__(self) -> None:
        self.windows: list[tuple[datetime, datetime]] = []

    async def get_fills(
        self, start_time: datetime, end_time: datetime, symbol: str | None = None
    ) -> list[Fill]:
        self.windows.append((start_time, end_time))
        if len(self.windows) > self.LIMIT:
            raise AssertionError("Разбиение на окна не завершается")
        return []


@pytest.mark.parametrize(
    "period",
    [
        timedelta(hours=3),                 # короче одного окна
        WINDOW,                             # ровно одно окно
        WINDOW + OVERLAP / 2,               # остаток меньше перекрытия
        WINDOW * 2,
        timedelta(days=365),
    ],
)
async def test_window_split_terminates(period: timedelta) -> None:
    exchange = CountingExchange()
    importer = HistoryImporter(exchange, trades=None, user_id=1)  # type: ignore[arg-type]

    fills, errors = await asyncio.wait_for(
        importer.fetch_fills(BASE, BASE + period), timeout=5
    )

    assert fills == []
    assert errors == []
    assert exchange.windows, "должно быть хотя бы одно окно"
    # Последнее окно обязано доходить до конца периода, иначе часть
    # истории молча не импортируется.
    assert exchange.windows[-1][1] == BASE + period


async def test_windows_overlap_to_avoid_gaps() -> None:
    """Соседние окна должны перекрываться: исполнение на границе иначе
    может не попасть ни в одно из них."""
    exchange = CountingExchange()
    importer = HistoryImporter(exchange, trades=None, user_id=1)  # type: ignore[arg-type]

    await importer.fetch_fills(BASE, BASE + WINDOW * 3)

    assert len(exchange.windows) >= 3
    for previous, current in zip(
        exchange.windows, exchange.windows[1:], strict=False
    ):
        assert current[0] < previous[1]


class FillsExchange:
    name = "bingx"

    def __init__(self, fills: list[Fill]) -> None:
        self.fills = fills

    async def get_fills(
        self, start_time: datetime, end_time: datetime, symbol: str | None = None
    ) -> list[Fill]:
        return self.fills


def _fill(external_id: str | None) -> Fill:
    return Fill(
        external_id=external_id, symbol="SOL-USDT", side=TradeSide.LONG, is_entry=True,
        price=Decimal("123.021"), quantity=Decimal("1362"), fee=Decimal("83.78152"),
        executed_at=BASE,
    )


async def test_fill_without_external_id_is_not_a_dedupe_key() -> None:
    """п.7, 29.09: дедуп только по непустому external_id — исполнение без
    идентификатора в выборку не попадает (WARNING пишет разбор)."""
    importer = HistoryImporter(
        FillsExchange([_fill("2104213344135159808"), _fill(None)]),  # type: ignore[arg-type]
        trades=None, user_id=1,  # type: ignore[arg-type]
    )

    fills, errors = await importer.fetch_fills(BASE, BASE + timedelta(hours=1))

    assert [f.external_id for f in fills] == ["2104213344135159808"]
    assert errors == []
