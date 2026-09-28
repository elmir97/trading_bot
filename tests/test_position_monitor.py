"""Монитор приближения к TP/SL (шаг 15.6): гистерезис и формат процента.

Живой случай — SOL #4, 27.09: вход 123.021, стоп 121.646, цена ходила
121.66–121.79 и за стоп; монитор слал «приближается к Stop-Loss» каждые
35–60 минут, процент — десятками знаков."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from app.database.models.trade import Trade
from app.database.models.user import User
from app.exchanges.bingx import BingXClient
from app.trading.enums import TradeSide
from app.workers.positions import PositionMonitor

D = Decimal


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent.append(text)


def _sol() -> Trade:
    return Trade(
        user=User(telegram_id=1), symbol="SOL-USDT", side=TradeSide.LONG,
        entry_price=D("123.021"), stop_loss=D("121.646"), take_profit=D("126.077"),
        quantity=D("1362.07"),
    )


async def _walk(prices: list[str]) -> list[str]:
    bot = FakeBot()
    monitor = PositionMonitor(bot, None, Settings())  # type: ignore[arg-type, call-arg]
    trade = _sol()
    for price in prices:
        await monitor._check_target(
            trade, D(price), target=trade.stop_loss, kind="sl", threshold=D("0.10")
        )
    return bot.sent


# Полоса «приближается» — последние 10% пути: 121.646..121.7835; сброс
# отметки — дальше 20% пути: выше 121.921.


async def test_jitter_at_band_edge_does_not_renotify() -> None:
    sent = await _walk(["121.69", "121.80", "121.70", "121.85", "121.66"])
    assert len(sent) == 1


async def test_price_beyond_stop_and_back_does_not_renotify() -> None:
    sent = await _walk(["121.69", "121.60", "121.66"])
    assert len(sent) == 1


async def test_moving_clearly_away_rearms() -> None:
    sent = await _walk(["121.69", "122.10", "121.69"])
    assert len(sent) == 2


async def test_percent_rounded_to_tenth() -> None:
    [text] = await _walk(["121.69"])
    assert "Осталось: 3.2% пути от входа" in text
    assert "Mark price: 121.69" in text


async def test_mark_price_parsed_from_live_premium_index_form() -> None:
    """Живая форма premiumIndex (27.09): data — объект, markPrice — строка."""
    payload = {
        "symbol": "SOL-USDT", "markPrice": "123.108", "indexPrice": "123.176",
        "lastFundingRate": "0.0001", "nextFundingTime": 1790553600000,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "", "data": payload})

    client = BingXClient(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://test"
    ))
    assert await client.get_mark_price("SOL-USDT") == D("123.108")
    with pytest.raises(Exception, match="нет записи"):
        await client.get_mark_price("LINK-USDT")
    await client.close()


class NetworkFailBot(FakeBot):
    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        from aiogram.exceptions import TelegramNetworkError

        raise TelegramNetworkError(method=None, message="Request timeout error")  # type: ignore[arg-type]


async def test_failed_send_leaves_mark_and_next_cycle_retries() -> None:
    """28.09: отметка приближения — только после доставки. Сбой сети не
    ставит её, и следующий цикл при той же цене шлёт снова."""
    trade = _sol()
    failing = PositionMonitor(NetworkFailBot(), None, Settings())  # type: ignore[arg-type, call-arg]
    await failing._check_target(
        trade, D("121.69"), target=trade.stop_loss, kind="sl", threshold=D("0.10")
    )
    assert trade.sl_approach_notified_at is None

    bot = FakeBot()
    monitor = PositionMonitor(bot, None, Settings())  # type: ignore[arg-type, call-arg]
    await monitor._check_target(
        trade, D("121.69"), target=trade.stop_loss, kind="sl", threshold=D("0.10")
    )
    assert len(bot.sent) == 1
    assert trade.sl_approach_notified_at is not None
