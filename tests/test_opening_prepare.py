"""Карточка открытия (OpeningService.prepare/preview) и схема M5 против
настоящей БД и фейковой биржи (tests/opening_fakes.py)."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.database.models.execution_callback import ExecutionCallback
from app.database.models.execution_order import ExecutionOrder
from app.database.models.trade import Trade
from app.database.models.trade_opening import TradeOpening
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import MarginType, Position
from app.execution.opening.calc import Issue, Level, OpeningInputs
from app.execution.opening.checks import classify, merge
from app.execution.opening.service import OpeningService
from app.market.cache import TTLCache
from app.services.user_service import UserService
from app.trading.enums import (
    EntryType,
    ExchangeKeyMode,
    ExecutionCallbackAction,
    OpeningSource,
    OpeningStatus,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    TradeSide,
    TradeSource,
)
from app.trading.risk import PlanCheck, Violation, ViolationCode
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
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True, bingx_trading_mode="demo", exec_dry_run=False,
    )
    db = Database(settings)
    exchange = OpeningExchange()
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
        plan = await UserRepository(session).get_trading_plan(user.id)
        assert plan is not None
        plan.risk_per_trade_percent = D(2)
        plan.max_leverage = 20
        plan.min_risk_reward = D("1.5")
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
            return OpeningService(session, s, None, user, factory=Factory(),
                                  market_cache=TTLCache())

        yield SimpleNamespace(session=session, user=user, uid=uid, exchange=exchange,
                              service=service, plan=plan)
        await session.rollback()
        fresh = await UserRepository(session).get_by_id(uid)
        await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


# --- checks: уровни нарушений плана (решение владельца 05.10) ---------------------


def test_plan_violation_levels() -> None:
    check = PlanCheck(violations=[
        Violation(ViolationCode.NO_STOP_LOSS, "нет стопа"),
        Violation(ViolationCode.RISK_TOO_HIGH, "риск"),
        Violation(ViolationCode.DAILY_LOSS_LIMIT, "день", is_blocking=True),
        Violation(ViolationCode.WEEKLY_LOSS_LIMIT, "неделя", is_blocking=True),
        Violation(ViolationCode.LEVERAGE_TOO_HIGH, "плечо"),
        Violation(ViolationCode.LOW_RISK_REWARD, "rr"),
        Violation(ViolationCode.SYMBOL_NOT_ALLOWED, "символ"),
        Violation(ViolationCode.DAILY_TRADE_LIMIT, "сделок"),
        Violation(ViolationCode.TIMEFRAME_NOT_ALLOWED, "тф"),
    ])
    levels = {i.code: i.level for i in classify(check)}
    assert levels == {
        "PLAN_DAILY_LOSS_LIMIT": Level.BLOCK,
        "PLAN_WEEKLY_LOSS_LIMIT": Level.BLOCK,
        "PLAN_LEVERAGE_TOO_HIGH": Level.BLOCK,
        "PLAN_LOW_RISK_REWARD": Level.WARN,
        "PLAN_SYMBOL_NOT_ALLOWED": Level.WARN,
        "PLAN_DAILY_TRADE_LIMIT": Level.WARN,
    }


def test_merge_blocks_first_and_dedup() -> None:
    warn = Issue("PLAN_LOW_RISK_REWARD", Level.WARN, "rr")
    block = Issue("STOP_WRONG_SIDE", Level.BLOCK, "стоп")
    assert merge((warn,), [block, warn]) == (block, warn)


# --- карточка -----------------------------------------------------------------------


async def test_card_row_snapshot(ctx) -> None:  # type: ignore[no-untyped-def]
    outcome = await ctx.service().prepare(
        INPUTS, source=OpeningSource.WIZARD, chat_id=7, message_id=55
    )
    assert outcome.can_open and outcome.has_warnings   # RR 1.00 < 1.5 — предупреждение
    row = outcome.opening
    assert row is not None and row.status is OpeningStatus.CARD
    assert row.quantity == D(323) and row.card_price == D("1.4950")
    assert row.risk_usd is not None and row.risk_usd.quantize(D("0.01")) == D("14.98")
    assert row.equity == D(1500) and row.leverage == 10 and row.margin_type == "ISOLATED"
    assert row.account_mode is ExchangeKeyMode.DEMO and row.source is OpeningSource.WIZARD
    assert row.chat_id == 7 and row.card_message_id == 55
    assert row.violations == [{
        "code": "PLAN_LOW_RISK_REWARD", "level": "WARN",
        "message": row.violations[0]["message"],
    }]
    text_ = outcome.text
    assert text_.startswith("🟢 Открыть на бирже — DEMO")
    assert "Риск: 14.98 $ (1.00% от 1500.00 $ equity), с комиссией" in text_
    assert "Комиссия ≈ 0.48 $ (вход и выход по стопу)" in text_
    assert "Объём: 323 XRP (≈482.89 $)" in text_
    assert "Маржа: 48.29 $ (изолированная, плечо 10x) · свободно 1475.00 $" in text_
    assert "Ликвидация ≈ 1.3605 (в 2.9 раза дальше стопа)" in text_
    assert "⚠️ RR" in text_ and "🧪 Сухой прогон" in text_
    assert not ctx.exchange.posts()   # карточка на биржу ничего не отправляет


async def test_blocking_issue_stored_as_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    outcome = await ctx.service().prepare(
        replace(INPUTS, leverage=20), source=OpeningSource.WIZARD
    )
    assert not outcome.can_open
    row = outcome.opening
    assert row is not None and row.status is OpeningStatus.REFUSED
    assert row.error_code == "LIQ_TOO_CLOSE" and "максимум 18x" in (row.error_message or "")
    assert "Карточка действует" not in outcome.text and "🚫" in outcome.text


async def test_position_on_side_blocks(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.positions["LONG"] = Position(
        "XRP-USDT", TradeSide.LONG, D(330), D("1.4952"), D("1.495"), 10, D(0), position_id="1",
    )
    outcome = await ctx.service().prepare(INPUTS, source=OpeningSource.MINIAPP)
    assert outcome.opening is not None and outcome.opening.error_code == "POSITION_EXISTS"


async def test_plan_leverage_and_daily_loss_block(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.plan.max_leverage = 5
    await ctx.session.commit()
    outcome = await ctx.service().prepare(INPUTS, source=OpeningSource.WIZARD)
    codes = {i.code for i in outcome.issues}
    assert "PLAN_LEVERAGE_TOO_HIGH" in codes and not outcome.can_open


async def test_margin_type_default_from_settings(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.user.settings.margin_type_default = "CROSSED"
    await ctx.session.commit()
    outcome = await ctx.service().preview(INPUTS)
    assert outcome.opening is None
    assert outcome.market is not None
    assert outcome.market.desired_margin_type is MarginType.CROSSED
    assert "кросс" in outcome.text and "проверится после входа" in outcome.text


async def test_preview_writes_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    await ctx.service().preview(INPUTS)
    rows = list(await ctx.session.scalars(
        select(TradeOpening).where(TradeOpening.user_id == ctx.uid)
    ))
    assert rows == []


async def test_live_refused_without_open_flag(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.user.settings.active_exchange_mode = ExchangeKeyMode.LIVE
    await ctx.session.commit()
    outcome = await ctx.service(
        bingx_trading_mode="live", exec_allow_live_mode_orders=True
    ).prepare(INPUTS, source=OpeningSource.WIZARD)
    assert outcome.refusal is not None
    assert "EXEC_OPEN_ALLOW_LIVE" in outcome.text
    assert outcome.opening is not None and outcome.opening.status is OpeningStatus.REFUSED


async def test_exchange_failure_is_refusal(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.fail_reads = {"balance"}
    outcome = await ctx.service().prepare(INPUTS, source=OpeningSource.WIZARD)
    assert outcome.refusal is not None and outcome.text.startswith("⛔ Биржа не ответила")
    assert outcome.opening is not None and outcome.opening.status is OpeningStatus.REFUSED


async def test_unknown_symbol(ctx) -> None:  # type: ignore[no-untyped-def]
    outcome = await ctx.service().prepare(
        OpeningInputs(symbol="NOPE-USDT", side=TradeSide.LONG, entry_type=EntryType.MARKET,
                      stop_loss=D(1), risk_percent=D(1), leverage=5),
        source=OpeningSource.WIZARD,
    )
    assert outcome.refusal is not None and "NOPE-USDT" in outcome.text


async def test_limit_card(ctx) -> None:  # type: ignore[no-untyped-def]
    outcome = await ctx.service().prepare(
        OpeningInputs(symbol="XRP-USDT", side=TradeSide.LONG, entry_type=EntryType.LIMIT,
                      limit_price=D("1.4800"), stop_loss=D("1.4500"), take_profit=D("1.5400"),
                      risk_percent=D(1), leverage=10, expiry_minutes=240),
        source=OpeningSource.WIZARD,
    )
    row = outcome.opening
    assert row is not None and row.status is OpeningStatus.CARD
    assert row.limit_price == D("1.4800") and row.expiry_minutes == 240
    assert "Лимит 1.48 · срок 4 ч" in outcome.text


# --- схема M5 -----------------------------------------------------------------------


async def test_schema_checks(ctx) -> None:  # type: ignore[no-untyped-def]
    base = dict(
        user_id=ctx.uid, source=OpeningSource.WIZARD, account_mode=ExchangeKeyMode.DEMO,
        symbol="XRP-USDT", side=TradeSide.LONG, stop_loss=D(1), risk_percent=D(1),
        leverage=5, margin_type="ISOLATED",
    )
    ctx.session.add(TradeOpening(**base, entry_type=EntryType.LIMIT))   # лимит без цены
    with pytest.raises(IntegrityError):
        await ctx.session.commit()
    await ctx.session.rollback()
    with pytest.raises(IntegrityError):
        await ctx.session.execute(text(
            "INSERT INTO trade_openings (user_id, source, account_mode, symbol, side, entry_type,"
            " stop_loss, risk_percent, leverage, margin_type, status) VALUES "
            f"({ctx.uid}, 'wizard', 'DEMO', 'XRP-USDT', 'LONG', 'MARKET', 1, 1, 5, 'ISOLATED',"
            " 'BOGUS')"
        ))
    await ctx.session.rollback()


async def test_links_and_new_values(ctx) -> None:  # type: ignore[no-untyped-def]
    opening = TradeOpening(
        user_id=ctx.uid, source=OpeningSource.MINIAPP, account_mode=ExchangeKeyMode.DEMO,
        symbol="XRP-USDT", side=TradeSide.LONG, entry_type=EntryType.LIMIT,
        limit_price=D("1.4"), stop_loss=D("1.3"), risk_percent=D(1), leverage=5,
        margin_type="ISOLATED", status=OpeningStatus.WORKING,
    )
    ctx.session.add(opening)
    await ctx.session.flush()
    order = ExecutionOrder(
        user_id=ctx.uid, trade_opening_id=opening.id, client_order_id=f"to{opening.id}u{ctx.uid}e",
        symbol="XRP-USDT", side=OrderSide.BUY, position_side=TradeSide.LONG,
        order_type=OrderType.LIMIT, role=OrderRole.ENTRY, status=OrderStatus.WORKING,
    )
    ctx.session.add(order)
    for action in (ExecutionCallbackAction.TO_YES_WARN, ExecutionCallbackAction.MA_CONFIRM):
        ctx.session.add(ExecutionCallback(
            user_id=ctx.uid, action=action.value, trade_opening_id=opening.id
        ))
    await ctx.session.commit()
    stored = await ctx.session.scalar(select(ExecutionOrder).where(ExecutionOrder.id == order.id))
    assert stored is not None and stored.status is OrderStatus.WORKING
    assert stored.order_type is OrderType.LIMIT and stored.trade_opening_id == opening.id
    # SET NULL: открытие удаляется — ордер остаётся аудитом
    await ctx.session.delete(opening)
    await ctx.session.commit()
    await ctx.session.refresh(stored)
    assert stored.trade_opening_id is None


async def test_bot_source_trade_roundtrip(ctx) -> None:  # type: ignore[no-untyped-def]
    """trades.source 'BOT' — VARCHAR без CHECK, пишется и читается."""
    trade = Trade(
        user_id=ctx.uid, symbol="XRP-USDT", side=TradeSide.LONG, entry_price=D("1.4952"),
        quantity=D(10), source=TradeSource.BOT, account_mode=ExchangeKeyMode.DEMO,
        opened_at=NOW,
    )
    ctx.session.add(trade)
    await ctx.session.commit()
    raw = await ctx.session.scalar(text(f"SELECT source FROM trades WHERE id = {trade.id}"))
    assert raw == "BOT"
    stored = await ctx.session.scalar(
        select(Trade).where(Trade.id == trade.id).execution_options(populate_existing=True)
    )
    assert stored is not None and stored.source is TradeSource.BOT
