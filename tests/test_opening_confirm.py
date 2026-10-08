"""«Открыть» (OpeningService.confirm), защита позиции и восстановление после
рестарта — против настоящей БД, FakeRedis и фейковой биржи с поведением BingX
по разведке Р1–Р8 (tests/opening_fakes.py)."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.locks import RedisLock, position_lock_key
from app.database.models.execution_order import ExecutionOrder
from app.database.models.trade import Trade
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import MarginType
from app.execution.opening.calc import OpeningInputs
from app.execution.opening.execution import Runner, opening_client_order_id
from app.execution.opening.recovery import recover_openings
from app.execution.opening.service import OpeningService
from app.market.cache import TTLCache
from app.services.user_service import UserService
from app.trading.enums import (
    EntryType,
    ExchangeKeyMode,
    FillSide,
    OpeningSource,
    OpeningStatus,
    OrderRole,
    OrderStatus,
    TradeSide,
    TradeSource,
    TradeStatus,
)
from tests.conftest import cleanup_user
from tests.opening_fakes import OpeningExchange

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)
INPUTS = OpeningInputs(
    symbol="XRP-USDT", side=TradeSide.LONG, entry_type=EntryType.MARKET,
    stop_loss=D("1.4501"), take_profit=D("1.5399"), risk_percent=D(1), leverage=10,
)


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    async for c in opening_context(unique_telegram_id()):
        yield c


async def opening_context(telegram_id: int):  # type: ignore[no-untyped-def]
    """Окружение открытия: пользователь с планом, фейковая биржа, FakeRedis,
    сервис и восстановление. Общий для test_opening_limits."""
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True, bingx_trading_mode="demo", exec_dry_run=False,
        exec_open_dry_run=False, exec_order_readback_delay_ms=0,
    )
    db = Database(settings)
    exchange = OpeningExchange()
    redis = FakeRedis(decode_responses=True)
    notes: list[tuple[int, str, Any]] = []
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=telegram_id)
        user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
        plan = await UserRepository(session).get_trading_plan(user.id)
        assert plan is not None
        plan.risk_per_trade_percent = D(2)
        plan.max_leverage = 20
        plan.min_risk_reward = D("0.5")   # без предупреждения RR — по умолчанию
        await session.commit()
        uid = user.id

        class Factory:
            async def get_credentials(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
                return SimpleNamespace(is_read_only=False, permissions_checked_at=NOW,
                                       user_id=user_id, mode=mode)

            async def for_user(self, session, user_id, exchange_name="bingx", mode=None):  # type: ignore[no-untyped-def]
                return exchange

        def service(**overrides: Any) -> OpeningService:
            s = settings.model_copy(update=overrides) if overrides else settings
            return OpeningService(session, s, None, user, redis=redis, factory=Factory(),
                                  market_cache=TTLCache())

        async def notify(telegram_id: int, text: str, position: Any) -> None:
            notes.append((telegram_id, text, position))

        async def recover(**kw: Any) -> int:
            return await recover_openings(db, settings, redis, Factory(), notify, **kw)

        yield SimpleNamespace(session=session, user=user, uid=uid, exchange=exchange,
                              service=service, redis=redis, plan=plan, notes=notes,
                              recover=recover, settings=settings, db=db, factory=Factory())
        await session.rollback()
        fresh = await UserRepository(session).get_by_id(uid)
        await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


async def _card(c, inputs: OpeningInputs = INPUTS, **settings: Any):  # type: ignore[no-untyped-def]
    outcome = await c.service(**settings).prepare(
        inputs, source=OpeningSource.WIZARD, chat_id=1, message_id=55
    )
    assert outcome.opening is not None and outcome.opening.status is OpeningStatus.CARD, (
        outcome.text
    )
    return outcome.opening


async def _rows(c, opening_id: int) -> list[ExecutionOrder]:  # type: ignore[no-untyped-def]
    return list(await c.session.scalars(
        select(ExecutionOrder).where(ExecutionOrder.trade_opening_id == opening_id)
        .order_by(ExecutionOrder.id)
    ))


async def _trade(c, trade_id: int) -> Trade:  # type: ignore[no-untyped-def]
    trade = await TradeRepository(c.session).get(trade_id, c.uid)
    assert trade is not None
    return trade


def _posts(c) -> list[str]:  # type: ignore[no-untyped-def]
    return [name for name, _ in c.exchange.posts()]


# --- сухой прогон ---------------------------------------------------------------------


async def test_dry_run_sends_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    out = await ctx.service(exec_open_dry_run=True).confirm(
        opening.id, accept_warnings=False, message_id=55
    )
    assert out.status is OpeningStatus.DRY_RUN and "Ничего не отправлено" in out.text
    assert _posts(ctx) == []
    rows = await _rows(ctx, opening.id)
    assert [r.role for r in rows] == [OrderRole.ENTRY, OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT]
    assert {r.status for r in rows} == {OrderStatus.DRY_RUN}
    assert rows[0].client_order_id == opening_client_order_id(opening.id, ctx.uid, "e")


# --- маркет: норма --------------------------------------------------------------------


async def test_market_happy_path(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False, message_id=55)
    assert out.status is OpeningStatus.DONE, out.text
    # плечо 20 → 10 до входа, с read-back; режим маржи уже изолированный
    assert _posts(ctx) == ["post_leverage", "post_market"]
    assert ctx.exchange.leverage["LONG"] == 10
    entry_kw = ctx.exchange.posts()[1][1]
    assert entry_kw["client_order_id"] == f"to{opening.id}u{ctx.uid}e"
    assert entry_kw["stop_loss"].trigger_price == D("1.4501")
    assert entry_kw["take_profit"].trigger_price == D("1.5399")
    assert entry_kw["quantity"] == D(323)

    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE and opening.trade_id == out.trade_id
    assert opening.avg_price == D("1.4950") and opening.filled_qty == D(323)
    rows = await _rows(ctx, opening.id)
    roles = {r.role: r for r in rows}
    assert roles[OrderRole.ENTRY].status is OrderStatus.FILLED
    assert roles[OrderRole.STOP_LOSS].exchange_order_id is not None
    assert roles[OrderRole.STOP_LOSS].client_order_id is None   # вложенный: cid пустой
    assert roles[OrderRole.TAKE_PROFIT].exchange_order_id is not None
    assert all(r.trade_id == out.trade_id for r in rows)

    trade = await _trade(ctx, out.trade_id)
    assert trade.source is TradeSource.BOT and trade.status is TradeStatus.OPEN
    assert trade.account_mode is ExchangeKeyMode.DEMO and trade.fill_confirmed
    assert trade.account_balance_at_entry == D(1500)
    assert trade.external_position_id == opening.position_id
    assert trade.initial_stop_loss == D("1.4501") and trade.leverage == 10
    entry = [f for f in trade.fills if f.fill_side is FillSide.ENTRY]
    assert len(entry) == 1 and entry[0].external_fill_id == opening.entry_order_id
    assert "✅ Открыто: XRP-USDT LONG 323 @ 1.495" in out.text
    assert f"сделка #{out.trade_id}" in out.text and "тейк 1.5399 ✓" in out.text


async def test_bot_trade_skipped_by_import(ctx) -> None:  # type: ignore[no-untyped-def]
    """§5 п.11: импорт не заводит сделку второй раз — orderId входа и стопа
    известны (execution_orders, external_fill_id сделки BOT)."""
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    await ctx.session.refresh(opening)
    order_ids = await ExecutionOrderRepository(ctx.session).exchange_order_ids(ctx.uid)
    fill_ids = await TradeRepository(ctx.session).bot_fill_external_ids(ctx.uid)
    assert opening.entry_order_id in order_ids and opening.entry_order_id in fill_ids
    stop = next(r for r in await _rows(ctx, opening.id) if r.role is OrderRole.STOP_LOSS)
    assert stop.exchange_order_id in order_ids
    assert out.trade_id is not None
    unresolved = await ExecutionOrderRepository(ctx.session).list_unresolved_entries(ctx.uid)
    assert unresolved == []


# --- кнопка и карточка --------------------------------------------------------------------


async def test_warnings_need_explicit_button(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.plan.min_risk_reward = D("1.5")
    await ctx.session.commit()
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert not out.final and "Открыть всё равно" in out.text
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.CARD and _posts(ctx) == []
    out = await ctx.service().confirm(opening.id, accept_warnings=True)
    assert out.status is OpeningStatus.DONE
    await ctx.session.refresh(opening)
    assert opening.warnings_accepted


async def test_repeat_confirm_sends_once(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    first = await ctx.service().confirm(opening.id, accept_warnings=False)
    second = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert first.status is OpeningStatus.DONE
    assert second.text == "Сделка уже открыта." and second.trade_id == first.trade_id
    assert not second.final
    assert _posts(ctx).count("post_market") == 1


async def test_parallel_confirm_sends_once(ctx) -> None:  # type: ignore[no-untyped-def]
    """Двойной тап / чат и Mini App одновременно — две сессии (два процесса
    или два апдейта): один вход."""
    opening = await _card(ctx)

    async def tap() -> Any:
        async with ctx.db.session() as session:
            user = await session.scalar(
                select(User).where(User.id == ctx.uid).options(selectinload(User.settings))
            )
            service = OpeningService(session, ctx.settings, None, user, redis=ctx.redis,
                                     factory=ctx.factory, market_cache=TTLCache())
            return await service.confirm(opening.id, accept_warnings=False)

    results = await asyncio.gather(tap(), tap())
    assert _posts(ctx).count("post_market") == 1
    assert sum(r.status is OpeningStatus.DONE for r in results) == 1
    # второе нажатие карточку не трогает: итог показывает первое
    assert sum(r.final for r in results) == 1


async def test_lock_busy(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    async with RedisLock(ctx.redis, position_lock_key(ctx.uid, "XRP-USDT", "LONG"), 30):
        out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert not out.final and "Уже идёт действие" in out.text
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.CARD


async def test_card_expired(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    await ctx.session.execute(
        update(TradeOpening).where(TradeOpening.id == opening.id)
        .values(created_at=NOW - timedelta(minutes=5))
    )
    await ctx.session.commit()
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.EXPIRED_CARD and _posts(ctx) == []


async def test_not_last_card(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False, message_id=999)
    assert not out.final and _posts(ctx) == []


async def test_decline(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    assert "ничего не отправлено" in await ctx.service().decline(opening.id)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.text == "Открытие отменено." and _posts(ctx) == []


async def test_price_drift_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    ctx.exchange.last = D("1.4700")   # дистанция до стопа ×0.44 — объём вырос вдвое
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.REFUSED and "Цена ушла с карточки" in out.text
    assert _posts(ctx) == []


# --- плечо и режим маржи -------------------------------------------------------------------


async def test_leverage_readback_mismatch_refuses_before_entry(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    ctx.exchange.leverage_readback_off = True
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.REFUSED
    assert "Плечо не выставилось: 20x вместо 10x" in out.text
    assert "post_market" not in _posts(ctx)


async def test_margin_type_switched_with_readback(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.user.settings.margin_type_default = "CROSSED"
    await ctx.session.commit()
    opening = await _card(ctx)
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DONE, out.text
    assert _posts(ctx)[:2] == ["post_margin_type", "post_leverage"]
    assert ctx.exchange.margin_type is MarginType.CROSSED


async def test_margin_readback_mismatch(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.user.settings.margin_type_default = "CROSSED"
    await ctx.session.commit()
    opening = await _card(ctx)
    ctx.exchange.margin_readback_off = True
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.REFUSED and "не сменился" in out.text
    assert "post_market" not in _posts(ctx)


# --- отказ и нет ответа ---------------------------------------------------------------------


async def test_exchange_rejects_entry(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    ctx.exchange.reject_entry_code = 101204
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.REJECTED and "Позиция не открыта" in out.text
    rows = await _rows(ctx, opening.id)
    assert rows[0].status is OrderStatus.REJECTED and rows[0].error_code == "101204"
    assert not ctx.exchange.positions


async def test_timeout_after_accept_resolved_by_cid(ctx) -> None:  # type: ignore[no-untyped-def]
    """Ответа нет, а ордер принят: поиск по cid находит исполнение — без повтора."""
    opening = await _card(ctx)
    ctx.exchange.timeout_entry = True
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DONE, out.text
    assert _posts(ctx).count("post_market") == 1


async def test_timeout_not_placed_then_recovery(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    ctx.exchange.timeout_entry = True
    ctx.exchange.timeout_after_accept = False
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.UNKNOWN and "Повторно вход не отправляется" in out.text
    # рано — ещё ждём
    assert await ctx.recover() == 0
    ctx.exchange.timeout_entry = False
    await ctx.recover(now=datetime.now(UTC) + timedelta(seconds=40))
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.NOT_PLACED
    assert _posts(ctx).count("post_market") == 1      # повторного входа не было
    assert "не выставлен на бирже" in ctx.notes[-1][1]


# --- защита ----------------------------------------------------------------------------------


async def test_attached_stop_missing_fallback_stop(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    ctx.exchange.drop_attached_sl = True
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DONE and "поставлен отдельным ордером" in out.text
    kw = next(kw for name, kw in ctx.exchange.posts() if name == "post_conditional")
    assert kw["close_position"] is True and kw["quantity"] == D(323)
    assert kw["client_order_id"] == f"to{opening.id}u{ctx.uid}s1"
    stops = [r for r in await _rows(ctx, opening.id) if r.role is OrderRole.STOP_LOSS]
    assert stops[-1].status is OrderStatus.SUBMITTED


async def test_stop_fails_emergency_close(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    ctx.exchange.drop_attached_sl = True
    ctx.exchange.fail_conditional = True
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.EMERGENCY_CLOSED, out.text
    assert "🚨 Аварийное закрытие: стоп не встал" in out.text
    assert not ctx.exchange.positions
    trade = await _trade(ctx, out.trade_id)
    assert trade.status is TradeStatus.CLOSED
    assert {f.fill_side for f in trade.fills} == {FillSide.ENTRY, FillSide.EXIT}


async def test_close_fails_alarm_then_recovery(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    ctx.exchange.drop_attached_sl = True
    ctx.exchange.fail_conditional = True
    ctx.exchange.fail_close = True
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.ALARM and "БЕЗ СТОПА" in out.text
    assert ctx.exchange.positions   # позиция осталась
    # следующий цикл: стоп встаёт — сделка записывается
    ctx.exchange.fail_conditional = False
    assert await ctx.recover() == 1
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE and opening.trade_id is not None
    assert "✅ Открыто" in ctx.notes[-1][1] and ctx.notes[-1][2] == ("XRP-USDT", TradeSide.LONG)


async def test_take_missing_is_warning(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    ctx.exchange.drop_attached_tp = True
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DONE
    assert "⚠️ не встал" in out.text and "поставь его в «Позиции»" in out.text


async def test_liquidation_before_stop_closes(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    ctx.exchange.liquidation_override = D("1.47")   # между входом 1.495 и стопом 1.4501
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.EMERGENCY_CLOSED
    assert "ликвидация ближе стопа" in out.text


# --- восстановление после рестарта ----------------------------------------------------------


async def test_recovery_confirmed_without_entry(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    await ctx.session.execute(
        update(TradeOpening).where(TradeOpening.id == opening.id)
        .values(status=OpeningStatus.CONFIRMED)
    )
    await ctx.session.commit()
    assert await ctx.recover() == 1
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.REFUSED and opening.error_code == "INTERRUPTED"
    assert _posts(ctx) == [] and "прервано перезапуском" in ctx.notes[-1][1]


async def test_recovery_after_fill_before_protection(ctx) -> None:  # type: ignore[no-untyped-def]
    """Падение между исполнением входа и проверкой стопа: восстановление
    находит стоп и записывает сделку, вход не повторяет."""
    opening = await _card(ctx)
    await ctx.session.execute(
        update(TradeOpening).where(TradeOpening.id == opening.id)
        .values(status=OpeningStatus.CONFIRMED)
    )
    await ctx.session.commit()
    await ctx.session.refresh(opening)
    runner = Runner(ctx.session, ctx.settings, ctx.exchange, opening,
                    price_precision=4, quantity_precision=0)
    entry = await runner.place_entry(D(323))
    assert entry.status is OpeningStatus.FILLED
    fill, _ = await runner.read_fill()
    assert fill is not None and await runner.apply_fill(fill)
    # «рестарт»
    assert await ctx.recover() == 1
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.DONE and opening.trade_id is not None
    assert _posts(ctx).count("post_market") == 1
    assert ctx.notes[-1][2] == ("XRP-USDT", TradeSide.LONG)


async def test_recovery_skips_locked(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx)
    await ctx.session.execute(
        update(TradeOpening).where(TradeOpening.id == opening.id)
        .values(status=OpeningStatus.CONFIRMED)
    )
    await ctx.session.commit()
    async with RedisLock(ctx.redis, position_lock_key(ctx.uid, "XRP-USDT", "LONG"), 30):
        assert await ctx.recover() == 0
    await ctx.session.refresh(opening)
    assert opening.status is OpeningStatus.CONFIRMED


async def test_short_market(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = await _card(ctx, replace(
        INPUTS, side=TradeSide.SHORT, stop_loss=D("1.5399"), take_profit=D("1.4501")
    ))
    out = await ctx.service().confirm(opening.id, accept_warnings=False)
    assert out.status is OpeningStatus.DONE, out.text
    kw = next(kw for name, kw in ctx.exchange.posts() if name == "post_market")
    assert kw["side"].value == "SELL" and kw["position_side"] == "SHORT"
