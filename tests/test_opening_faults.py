"""Аварийные ветки открытия и управляемый сбой EXEC_OPEN_FAULT (08.10.2026).

Правило решения «стоп не встал» (решение владельца): окончательно — только POST
отклонён кодом биржи ИЛИ ордер по своему clientOrderId не найден / снят. Нет в
openOrders, а по cid ответа нет — ALARM «не подтверждён», без закрытия,
перепроверка циклом. Перед новым запасным стопом — прежние по cid.

Т1–Т5 — сценарии проверки на демо (план 08.10), здесь на фейковой бирже.
Фикстура — test_opening_confirm.opening_context."""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.core.config import Settings
from app.execution.opening.calc import OpeningInputs
from app.trading.enums import (
    CancelSource,
    OpeningSource,
    OpeningStatus,
    OrderRole,
    OrderStatus,
    TradeStatus,
)
from tests.test_opening_confirm import INPUTS, _card, _rows, _trade, opening_context

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
BASE = dict(
    bot_token="1:x", database_url="postgresql+asyncpg://u:p@h/d", encryption_key="0" * 43 + "="
)


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    async for c in opening_context(unique_telegram_id()):
        yield c


async def _open(c, fault: str = "", inputs: OpeningInputs = INPUTS):  # type: ignore[no-untyped-def]
    opening = await _card(c, inputs)
    out = await c.service(exec_open_fault=fault).confirm(opening.id, accept_warnings=False)
    await c.session.refresh(opening)
    return opening, out


async def _recover(c, fault: str = "") -> int:  # type: ignore[no-untyped-def]
    """Следующий цикл восстановления (настройки сбоя — те же, что у процесса)."""
    from app.execution.opening.recovery import recover_openings

    settings = c.settings.model_copy(update={"exec_open_fault": fault})

    async def notify(telegram_id: int, text: str, position: Any, **_: Any) -> None:
        c.notes.append((telegram_id, text, position))

    return await recover_openings(c.db, settings, c.redis, c.factory, notify)


def _posts(c, name: str) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """POST по имени; условные ордера — только стопы (тейк заменяется отдельно)."""
    return [
        kw for n, kw in c.exchange.posts()
        if n == name and not (n == "post_conditional" and kw["order_type"] != "STOP_MARKET")
    ]


def _stops_on_exchange(c) -> list[Any]:  # type: ignore[no-untyped-def]
    return [o for o in c.exchange.orders if o.order_type == "STOP_MARKET"]


# --- настройки: только демо ---------------------------------------------------------------


def test_fault_values_and_default() -> None:
    assert Settings(**BASE).exec_open_faults == frozenset()  # type: ignore[arg-type]
    s = Settings(**BASE, exec_open_fault="skip_attached_stop, hide_backup_stop")  # type: ignore[arg-type]
    assert s.exec_open_faults == {"skip_attached_stop", "hide_backup_stop"}
    with pytest.raises(ValidationError, match="неизвестные сбои"):
        Settings(**BASE, exec_open_fault="drop_everything")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "live", [
        {"bingx_trading_mode": "live"},
        {"exec_open_allow_live": True},
        {"exec_allow_live_mode_orders": True},
    ],
)
def test_fault_refused_with_any_live_sign(live: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="только на демо"):
        Settings(**BASE, exec_open_fault="fail_backup_stop", **live)  # type: ignore[arg-type]


async def test_startup_refused_and_logged(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """Запрещённая конфигурация — бот не стартует, причина в логе."""
    from app import main as app_main

    try:
        Settings(**BASE, exec_open_fault="fail_backup_stop", bingx_trading_mode="live")  # type: ignore[arg-type]
    except ValidationError as exc:
        error = exc

    def refuse() -> Settings:
        raise error

    monkeypatch.setattr(app_main, "get_settings", refuse)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as stop:
        await app_main.run()
    assert stop.value.code == 2
    assert any("Настройки отклонены" in r.getMessage() for r in caplog.records)


async def test_service_refuses_live_with_fault(ctx) -> None:  # type: ignore[no-untyped-def]
    """Вторая защита — в ядре (настройки в обход проверки старта)."""
    from app.trading.enums import ExchangeKeyMode

    ctx.user.settings.active_exchange_mode = ExchangeKeyMode.LIVE
    await ctx.session.commit()
    outcome = await ctx.service(
        bingx_trading_mode="live", exec_allow_live_mode_orders=True, exec_open_allow_live=True,
        exec_open_fault="skip_attached_stop",
    ).prepare(INPUTS, source=OpeningSource.WIZARD)
    assert outcome.refusal is not None and "управляемый сбой" in outcome.text


async def test_fault_warning_hourly(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import AsyncMock

    from app.workers import openings
    from app.workers.openings import OpeningsWorker

    monkeypatch.setattr(openings, "expire_stale_cards", AsyncMock(return_value=0))

    settings = Settings(**BASE, exec_open_fault="skip_attached_stop")  # type: ignore[arg-type]
    worker = OpeningsWorker(None, None, settings, None, None, factory=object())  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="app.workers.openings"):
        await worker.run()
        await worker.run()
    warned = [r for r in caplog.records if "EXEC_OPEN_FAULT включён" in r.getMessage()]
    assert len(warned) == 1


# --- Т1–Т5 -----------------------------------------------------------------------------------


async def test_t1_skip_attached_stop_backup_stop(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, out = await _open(ctx, "skip_attached_stop")
    assert out.status is OpeningStatus.DONE
    assert "стоп 1.4501 ✓ (на всю позицию, поставлен отдельным ордером" in out.text
    assert _posts(ctx, "post_market")[0]["stop_loss"] is None      # вход без стопа
    [stop] = _stops_on_exchange(ctx)
    assert stop.close_position and stop.client_order_id == f"to{opening.id}u{ctx.uid}s1"
    rows = [r for r in await _rows(ctx, opening.id) if r.role is OrderRole.STOP_LOSS]
    assert [(r.client_order_id, r.status) for r in rows] == [
        (f"to{opening.id}u{ctx.uid}s1", OrderStatus.SUBMITTED)
    ]


async def test_t2_backup_stop_fails_emergency_close(ctx) -> None:  # type: ignore[no-untyped-def]
    opening, out = await _open(ctx, "skip_attached_stop,fail_backup_stop")
    assert out.status is OpeningStatus.EMERGENCY_CLOSED and "🚨 Аварийное закрытие" in out.text
    assert _posts(ctx, "post_conditional") == []                 # s1 не отправлялся
    assert not ctx.exchange.positions and not ctx.exchange.orders
    rows = {r.role: r for r in await _rows(ctx, opening.id)}
    assert rows[OrderRole.STOP_LOSS].error_code == "FAULT"
    assert rows[OrderRole.CLOSE].status is OrderStatus.FILLED
    trade = await _trade(ctx, out.trade_id)
    assert trade.status is TradeStatus.CLOSED


async def test_t3_alarm_cycle_places_s2(ctx) -> None:  # type: ignore[no-untyped-def]
    fault = "skip_attached_stop,fail_backup_stop,fail_emergency_close"
    opening, out = await _open(ctx, fault)
    assert out.status is OpeningStatus.ALARM and "БЕЗ СТОПА" in out.text
    assert ctx.exchange.positions and not _stops_on_exchange(ctx)
    assert await _recover(ctx, fault) == 1                       # сбой — только 1-я попытка
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE
    [stop] = _stops_on_exchange(ctx)
    assert stop.client_order_id == f"to{opening.id}u{ctx.uid}s2"
    assert len(_posts(ctx, "post_conditional")) == 1
    assert _posts(ctx, "post_market")[1:] == []                  # закрытия не было


async def test_t4_hidden_stop_decided_by_cid_emergency(ctx) -> None:  # type: ignore[no-untyped-def]
    """Стоп принят, но скрыт и в openOrders, и по cid (честный худший случай):
    по cid «не найден» → окончательно → аварийное закрытие; биржа снимает s1."""
    opening, out = await _open(ctx, "skip_attached_stop,hide_backup_stop")
    assert out.status is OpeningStatus.EMERGENCY_CLOSED
    assert len(_posts(ctx, "post_conditional")) == 1             # s1 реально ушёл
    s1 = f"to{opening.id}u{ctx.uid}s1"
    assert ctx.exchange.by_cid[s1]["status"] == "CANCELLED"       # снят вместе с позицией
    assert not ctx.exchange.positions and not ctx.exchange.orders
    row = next(r for r in await _rows(ctx, opening.id) if r.client_order_id == s1)
    # A.3: s1 биржа приняла (orderId есть) — статус по факту после закрытия,
    # решение «не найден» остаётся в error_code историей (владелец 10.10).
    assert row.status is OrderStatus.CANCELLED and row.cancel_source is CancelSource.EXCHANGE
    assert row.error_code == "NOT_FOUND"


async def test_t5_alarm_cycle_hidden_stop_no_s2(ctx) -> None:  # type: ignore[no-untyped-def]
    """Главный случай: s1 стоит, но скрыт; закрытие «не прошло» → ALARM.
    Следующий цикл видит s1 — сделка без второго стопа."""
    fault = "skip_attached_stop,hide_backup_stop,fail_emergency_close"
    opening, out = await _open(ctx, fault)
    assert out.status is OpeningStatus.ALARM
    assert await _recover(ctx, fault) == 1
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE
    assert len(_posts(ctx, "post_conditional")) == 1             # s2 не было
    [stop] = _stops_on_exchange(ctx)
    assert stop.client_order_id == f"to{opening.id}u{ctx.uid}s1"
    rows = [r for r in await _rows(ctx, opening.id) if r.role is OrderRole.STOP_LOSS]
    assert [r.client_order_id for r in rows] == [f"to{opening.id}u{ctx.uid}s1"]
    assert rows[0].status is OrderStatus.SUBMITTED


async def test_t5b_alarm_cycle_stop_seen_only_by_cid(ctx) -> None:  # type: ignore[no-untyped-def]
    """То же, но в следующем цикле s1 всё ещё не в openOrders — находится
    запросом по cid, второй стоп не ставится."""
    fault = "skip_attached_stop,hide_backup_stop,fail_emergency_close"
    opening, _ = await _open(ctx, fault)
    ctx.exchange.conditional_invisible_in_list = True
    await _recover(ctx, fault)
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE
    assert len(_posts(ctx, "post_conditional")) == 1


# --- правило решения без сбоев -----------------------------------------------------------------


async def test_stop_not_in_list_but_found_by_cid_is_standing(ctx) -> None:  # type: ignore[no-untyped-def]
    """Нет в openOrders, по cid — NEW: стоит, позицию не закрываем."""
    ctx.exchange.drop_attached_sl = True
    ctx.exchange.conditional_invisible_in_list = True
    opening, out = await _open(ctx)
    assert out.status is OpeningStatus.DONE
    assert _posts(ctx, "post_market")[1:] == []
    assert len(_posts(ctx, "post_conditional")) == 1


async def test_unconfirmed_stop_alarm_without_close(ctx) -> None:  # type: ignore[no-untyped-def]
    """Нет в openOrders и по cid ответа нет — «не подтверждён»: ALARM, без
    закрытия; следующий цикл подтверждает по cid и пишет сделку без s2."""
    ctx.exchange.drop_attached_sl = True
    ctx.exchange.conditional_invisible_in_list = True
    opening = await _card(ctx)
    # Чтение по cid ломается после постановки запасного стопа (вход читается штатно).
    original_cond = ctx.exchange.place_conditional_order

    async def cond_then_fail_reads(**kw: Any):  # type: ignore[no-untyped-def]
        result = await original_cond(**kw)
        ctx.exchange.fail_reads = {"order_fill"}
        return result

    ctx.exchange.place_conditional_order = cond_then_fail_reads  # type: ignore[method-assign]
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.ALARM and "не подтверждён биржей" in out.text
    assert _posts(ctx, "post_market")[1:] == []                  # не закрывали
    assert ctx.exchange.positions
    ctx.exchange.fail_reads = set()
    assert await _recover(ctx) == 1
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE
    assert len(_posts(ctx, "post_conditional")) == 1             # без s2


async def test_backup_stop_rejected_by_code_is_final(ctx) -> None:  # type: ignore[no-untyped-def]
    """POST запасного стопа отклонён кодом биржи — окончательно, без запроса по cid."""
    ctx.exchange.drop_attached_sl = True
    ctx.exchange.fail_conditional = True
    _, out = await _open(ctx)
    assert out.status is OpeningStatus.EMERGENCY_CLOSED
    assert [n for n, p in ctx.exchange.calls if n == "order_fill" and p and "s1" in p] == []


# --- A.3 (10.10): строки стопа/тейка после аварийного закрытия и повторной проверки ---------


async def test_t2_attached_take_recorded_and_settled(ctx) -> None:  # type: ignore[no-untyped-def]
    """Хвост Т2: вложенный тейк записан сразу после поиска (до решения по
    стопу) и после аварийного закрытия — CANCELLED, снят биржей."""
    opening, out = await _open(ctx, "skip_attached_stop,fail_backup_stop")
    assert out.status is OpeningStatus.EMERGENCY_CLOSED
    [take] = [r for r in await _rows(ctx, opening.id) if r.role is OrderRole.TAKE_PROFIT]
    assert take.client_order_id is None and take.exchange_order_id
    assert take.status is OrderStatus.CANCELLED and take.cancel_source is CancelSource.EXCHANGE
    assert take.trade_id == out.trade_id


async def test_t5_recheck_clears_error_and_logs(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """Хвосты Т5 1–2: s1 найден на повторной проверке — SUBMITTED без ошибки
    первой попытки; в логе INFO «новый не ставлю» с прежним кодом."""
    fault = "skip_attached_stop,hide_backup_stop,fail_emergency_close"
    opening, _ = await _open(ctx, fault)
    s1 = f"to{opening.id}u{ctx.uid}s1"
    [row] = [r for r in await _rows(ctx, opening.id) if r.client_order_id == s1]
    assert row.status is OrderStatus.REJECTED and row.error_code == "NOT_FOUND"
    with caplog.at_level(logging.INFO):
        assert await _recover(ctx, fault) == 1
    await ctx.session.refresh(row)
    assert row.status is OrderStatus.SUBMITTED
    assert row.error_code is None and row.error_message is None
    [rec] = [r for r in caplog.records
             if r.getMessage() == "Ордер найден на повторной проверке — новый не ставлю"]
    assert (rec.cid, rec.prev_status, rec.prev_error) == (s1, "REJECTED", "NOT_FOUND")


def _stale_open_orders(c, reads: int):  # type: ignore[no-untyped-def]
    """openOrders после закрытия позиции ещё отдаёт её условники `reads` раз
    (биржа снимает их через десятки мс)."""
    original = c.exchange.get_open_orders
    state = {"last": [], "left": reads}

    async def read(symbol=None, *, max_retries=None):  # type: ignore[no-untyped-def]
        orders = await original(symbol, max_retries=max_retries)
        if c.exchange.positions:
            state["last"] = orders
            return orders
        if state["left"] > 0 and state["last"]:
            state["left"] -= 1
            return list(state["last"])
        return orders

    c.exchange.get_open_orders = read  # type: ignore[method-assign]


async def test_emergency_settle_retries_once_after_race(ctx) -> None:  # type: ignore[no-untyped-def]
    _stale_open_orders(ctx, reads=1)
    opening, out = await _open(ctx, "skip_attached_stop,fail_backup_stop")
    assert out.status is OpeningStatus.EMERGENCY_CLOSED
    [take] = [r for r in await _rows(ctx, opening.id) if r.role is OrderRole.TAKE_PROFIT]
    assert take.status is OrderStatus.CANCELLED


async def test_emergency_settle_leaves_standing_row(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """Ордер всё ещё виден после повтора — строку не трогаем (её доберёт
    обход закрытых сделок в reconciler), WARNING."""
    _stale_open_orders(ctx, reads=2)
    with caplog.at_level(logging.WARNING):
        opening, out = await _open(ctx, "skip_attached_stop,fail_backup_stop")
    assert out.status is OpeningStatus.EMERGENCY_CLOSED
    [take] = [r for r in await _rows(ctx, opening.id) if r.role is OrderRole.TAKE_PROFIT]
    assert take.status is OrderStatus.SUBMITTED and take.cancel_source is None
    assert any(r.getMessage().startswith("Условный ордер открытия ещё стоит")
               for r in caplog.records)
