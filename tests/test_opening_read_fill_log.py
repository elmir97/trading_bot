"""read_fill пишет причину каждой неудачной попытки (деплой 3, фикс 1): на Т3
10.10 вход «не подтверждён» 38 с без единой строки в логе — ExchangeError
глотался молча. Поля — по списку, ответ биржи целиком в лог не попадает."""

from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.exchanges.base import (
    ExchangeResponseError,
    ExchangeUnavailableError,
    OrderFill,
    ReadbackIncomplete,
)
from app.execution.opening.execution import Runner
from app.trading.enums import TradeSide

SECRET = "apiKey-эхо-не-печатать"


class Client:
    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers

    async def get_order_fill(self, symbol: str, cid: str, *, max_retries: int | None = None) -> Any:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fill(status: str, executed: str) -> OrderFill:
    return OrderFill(
        order_id="1", client_order_id="to1u1e", status=status, avg_price=Decimal(0),
        orig_qty=Decimal(202), executed_qty=Decimal(executed), fee=Decimal(0),
        raw={"apiKey": SECRET},
    )


def _runner(answers: list[Any]) -> Runner:
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True, bingx_trading_mode="demo",
        exec_order_readback_delay_ms=0,
    )
    opening = SimpleNamespace(id=1, user_id=1, symbol="XRP-USDT", side=TradeSide.LONG)

    async def no_sleep(_: float) -> None:
        return None

    return Runner(
        None, settings, Client(answers), opening,  # type: ignore[arg-type]
        price_precision=4, quantity_precision=0, sleep=no_sleep,
    )


def _records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(message)]


async def test_each_failed_attempt_logged_with_reason(caplog: pytest.LogCaptureFixture) -> None:
    incomplete = ReadbackIncomplete(
        "avgPrice", payload={"status": "FILLED", "avgPrice": "", "apiKey": SECRET}
    )
    runner = _runner([
        incomplete,
        ExchangeResponseError("BingX: order not exist (код 109421)", code=109421),
        ExchangeUnavailableError("BingX не ответил вовремя"),
    ])
    with caplog.at_level(logging.INFO):
        fill, not_found = await runner.read_fill()
    assert fill is None and not not_found
    tries = _records(caplog, "Вход не подтверждён по cid")
    got = [(r.attempt, r.error, r.code, getattr(r, "field", None), getattr(r, "status", None))  # type: ignore[attr-defined]
           for r in tries]
    assert got == [
        (1, "ReadbackIncomplete", None, "avgPrice", "FILLED"),
        (2, "ExchangeResponseError", 109421, None, None),
        (3, "ExchangeUnavailableError", None, None, None),
    ]
    final = _records(caplog, "Вход не подтверждён за 3 попыток")
    assert len(final) == 1 and final[0].levelno == logging.WARNING
    assert SECRET not in caplog.text
    assert all(SECRET not in str(r.__dict__) for r in caplog.records)


async def test_not_filled_answer_logged_with_status(caplog: pytest.LogCaptureFixture) -> None:
    runner = _runner([_fill("NEW", "0"), _fill("NEW", "0"), _fill("FILLED", "202")])
    with caplog.at_level(logging.INFO):
        fill, _ = await runner.read_fill()
    assert fill is not None and fill.status == "FILLED"
    tries = _records(caplog, "Вход не подтверждён по cid")
    assert [(r.attempt, r.status, r.executed_qty) for r in tries] == [  # type: ignore[attr-defined]
        (1, "NEW", "0"), (2, "NEW", "0"),
    ]
    assert _records(caplog, "Вход не подтверждён за") == []   # подтвердился — без WARNING
    assert SECRET not in str([r.__dict__ for r in caplog.records])


async def test_confirmed_first_try_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    runner = _runner([_fill("FILLED", "202")])
    with caplog.at_level(logging.INFO):
        await runner.read_fill()
    assert _records(caplog, "Вход не подтверждён") == []
