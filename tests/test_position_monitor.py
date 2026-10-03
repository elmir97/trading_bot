"""Монитор приближения к SL/TP (этап 5, app/workers/positions.py): позиции
биржи, порог на пользователя, дедуп по уровню, гистерезис, кнопки, лок.

Настоящая БД (position_alerts, user_settings), биржа и рынок — фейки с
состоянием. Живой случай гистерезиса — SOL #4, 27.09: цена ходила у края
полосы и за стоп, монитор слал «приближается» каждые 35–60 минут."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from fakeredis.aioredis import FakeRedis
from sqlalchemy import select

from app.core.config import Settings
from app.core.locks import position_lock_key
from app.database.models.position_alert import PositionAlert
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ExchangeAuthError, OpenOrder, Position, SymbolInfo
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode, TradeSide, TradeSource
from app.trading.journal import TradeJournal
from app.workers import positions as monitor_module
from app.workers.positions import PositionMonitor
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)
# Вход 100, стоп 90, тейк 120: 80% пути к стопу — 92, к тейку — 116.
ENTRY, STOP, TAKE = D(100), D(90), D(120)


def _order(oid: str, otype: str, price: Decimal, side: str = "LONG") -> OpenOrder:
    return OpenOrder(
        order_id=oid, client_order_id="", symbol="SOL-USDT",
        side="SELL" if side == "LONG" else "BUY", position_side=side, order_type=otype,
        quantity=D(2), executed_qty=D(0), price=D(0), stop_price=price, status="NEW",
        leverage=10, reduce_only=True, close_position=True, working_type="MARK_PRICE",
        created_at=NOW, updated_at=NOW, take_profit=None, stop_loss=None,
    )


class FakeExchange:
    def __init__(self) -> None:
        self.position: Position | None = Position(
            symbol="SOL-USDT", side=TradeSide.LONG, quantity=D(2), entry_price=ENTRY,
            mark_price=ENTRY, leverage=10, unrealized_pnl=D(0), liquidation_price=D(50),
            position_id="p1",
        )
        self.orders = [_order("s1", "STOP_MARKET", STOP), _order("t1", "TAKE_PROFIT_MARKET", TAKE)]
        self.calls: list[str] = []

    async def get_positions(self, *, max_retries=None) -> list[Position]:  # type: ignore[no-untyped-def]
        self.calls.append("positions")
        return [self.position] if self.position else []

    async def get_open_orders(self, symbol=None, *, max_retries=None) -> list[OpenOrder]:  # type: ignore[no-untyped-def]
        self.calls.append("open_orders")
        return list(self.orders)

    async def close(self) -> None: ...


class FakeMarket:
    def __init__(self) -> None:
        self.mark = ENTRY

    async def get_mark_prices(self, symbols: list[str]) -> dict[str, Decimal | None]:
        return dict.fromkeys(symbols, self.mark)

    async def get_symbols(self, *, max_retries=None) -> list[SymbolInfo]:  # type: ignore[no-untyped-def]
        return [SymbolInfo("SOL-USDT", 2, 2, D(1), D(1))]


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.fail = False

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        if self.fail:
            raise TelegramNetworkError(method=SendMessage(chat_id=chat_id, text=text), message="x")
        self.sent.append((text, reply_markup))


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings(  # type: ignore[call-arg]
        position_monitor_snapshot_seconds=0,   # снимок каждый цикл — тесты шагают явно
    )
    db = Database(settings)
    exchange, market, bot, redis = FakeExchange(), FakeMarket(), FakeBot(), FakeRedis()
    state = SimpleNamespace(auth_error=False, uid=None)

    class Factory:
        async def for_user(self, session, user_id, exchange_name="bingx", mode=None):  # type: ignore[no-untyped-def]
            # Ключи только у пользователя теста: в тестовой базе есть и чужие.
            if state.auth_error or user_id != state.uid:
                raise ExchangeAuthError("нет ключей")
            return exchange

    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
        await session.commit()
        uid = user.id
    state.uid = uid
    monitor = PositionMonitor(bot, db, settings, None, redis, factory=Factory(), market=market)  # type: ignore[arg-type]

    async def alerts() -> list[tuple[str, Decimal]]:
        async with db.session() as s:
            rows = await s.scalars(select(PositionAlert).where(PositionAlert.user_id == uid))
            return sorted((r.kind, r.level_price) for r in rows)

    async def step(mark: str) -> list[str]:
        market.mark = D(mark)
        before = len(bot.sent)
        await monitor.run()
        return [text for text, _ in bot.sent[before:]]

    async def set_user(**fields: Any) -> None:
        async with db.session() as s:
            st = await UserRepository(s).get_settings(uid)
            for k, v in fields.items():
                setattr(st, k, v)

    yield SimpleNamespace(db=db, uid=uid, exchange=exchange, market=market, bot=bot,
                          redis=redis, monitor=monitor, alerts=alerts, step=step,
                          set_user=set_user, state=state, settings=settings)
    async with db.session() as session:
        fresh = await UserRepository(session).get_by_id(uid)
        await cleanup_user(session, fresh)
    await db.dispose()


async def test_stop_80_percent_long(ctx) -> None:  # type: ignore[no-untyped-def]
    assert await ctx.step("92.5") == []          # 75% пути — рано
    [text] = await ctx.step("92")                # 80%
    assert "SOL-USDT LONG</b> приближается к стопу" in text
    assert "стоп 90" in text and "осталось 2.17%" in text
    assert "PnL -16.00 USDT" in text and "н/д" in text   # позиция не в журнале — R н/д
    assert await ctx.alerts() == [("SL", D(90))]


async def test_take_80_percent_and_short(ctx) -> None:  # type: ignore[no-untyped-def]
    [text] = await ctx.step("116")
    assert "приближается к тейку" in text and "PnL +32.00 USDT" in text
    ctx.exchange.position = replace(ctx.exchange.position, side=TradeSide.SHORT)
    ctx.exchange.orders = [_order("s2", "STOP_MARKET", D(110), "SHORT"),
                           _order("t2", "TAKE_PROFIT_MARKET", D(80), "SHORT")]
    [text] = await ctx.step("108")               # SHORT: 80% к стопу 110
    assert "SOL-USDT SHORT</b> приближается к стопу" in text


async def test_r_from_journal_initial_stop(ctx) -> None:  # type: ignore[no-untyped-def]
    async with ctx.db.session() as session:
        await TradeJournal(TradeRepository(session)).open_trade(
            user_id=ctx.uid, symbol="SOL-USDT", side=TradeSide.LONG, entry_price=ENTRY,
            quantity=D(2), stop_loss=D(90), source=TradeSource.IMPORTED,
            account_mode=ExchangeKeyMode.DEMO,
            external_position_id="p1", external_fill_id="f1", fee=D("0.1"),
        )
    [text] = await ctx.step("92")
    # 1R = 10 на единицу: осталось 2 → 0.20R; PnL −16 на 2 шт → −0.80R
    assert "(0.20R)" in text and "(-0.80R)" in text


async def test_disabled_and_threshold_per_user(ctx) -> None:  # type: ignore[no-untyped-def]
    await ctx.set_user(notifications={"sl_approaching": False}, tp_alert_percent=50)
    assert await ctx.step("91") == []            # стоп выключен
    [text] = await ctx.step("110")               # тейк: порог 50% — 110
    assert "приближается к тейку" in text


async def test_legacy_switch_off_disables_both(ctx) -> None:  # type: ignore[no-untyped-def]
    await ctx.set_user(notifications={"tp_sl_approaching": False})
    assert await ctx.step("91") == [] and await ctx.step("118") == []
    assert "positions" not in ctx.exchange.calls   # выключено всё — биржу не опрашиваем


async def test_hysteresis_and_dedup_by_level(ctx) -> None:  # type: ignore[no-untyped-def]
    """SOL #4: дребезг у края и уход за стоп — одно уведомление; повтор только
    после отката ниже порога − 20 п.п. (60% = 94) и нового подхода."""
    sent = []
    for mark in ("92", "92.4", "91.5", "89", "93", "92"):
        sent += await ctx.step(mark)
    assert len(sent) == 1
    assert await ctx.step("94.5") == []          # 55% — отметка снята
    assert await ctx.alerts() == []
    assert len(await ctx.step("92")) == 1        # новый подход — снова


async def test_moved_level_is_new_key(ctx) -> None:  # type: ignore[no-untyped-def]
    assert len(await ctx.step("92")) == 1
    ctx.exchange.orders[0] = _order("s3", "STOP_MARKET", D(91))   # стоп перенесли
    [text] = await ctx.step("91.4")              # 80% к 91 = 91.8 — пройдено
    assert "стоп 91" in text
    assert await ctx.alerts() == [("SL", D(91))]   # ключ старого уровня снят


async def test_position_gone_keys_removed(ctx) -> None:  # type: ignore[no-untyped-def]
    await ctx.step("92")
    ctx.exchange.position = None
    await ctx.step("92")
    assert await ctx.alerts() == []


async def test_failed_delivery_leaves_no_mark_and_retries(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.bot.fail = True
    await ctx.step("92")
    assert await ctx.alerts() == []
    ctx.bot.fail = False
    assert len(await ctx.step("92")) == 1
    assert await ctx.alerts() == [("SL", D(90))]


async def test_live_lock_skips_cycle(ctx) -> None:  # type: ignore[no-untyped-def]
    await ctx.redis.set(position_lock_key(ctx.uid, "SOL-USDT", "LONG"), "1", ex=30)
    assert await ctx.step("92") == []
    assert ctx.exchange.calls == []


async def test_no_keys_no_alerts(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.state.auth_error = True
    assert await ctx.step("92") == []


async def test_buttons_lead_to_stage4_cards(ctx) -> None:  # type: ignore[no-untyped-def]
    from app.bot.handlers.position_actions import ActionCB
    from app.bot.handlers.positions import PositionsCB

    assert monitor_module.ACTION_OPEN == ActionCB.OPEN
    assert monitor_module.POSITION_ACTIONS == PositionsCB.ACTIONS
    await ctx.step("92")                          # у стопа — безубытка нет
    _, keyboard = ctx.bot.sent[-1]
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == ["pa:cf:SOL-USDT:L", "pos:act:SOL-USDT:L"]
    await ctx.step("116")                         # у тейка mark за безубытком
    _, keyboard = ctx.bot.sent[-1]
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == ["pa:be:SOL-USDT:L", "pa:cf:SOL-USDT:L", "pos:act:SOL-USDT:L"]
    assert all(len(d.encode()) <= 64 for d in data)


# --- «перевёрнутые» уровни (02.10 18:00:48: стоп SHORT в безубытке) ----------


async def _as_short(ctx, *, stop: str | None = None, take: str | None = None) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.position = replace(ctx.exchange.position, side=TradeSide.SHORT)
    ctx.exchange.orders = (
        ([_order("ss", "STOP_MARKET", D(stop), "SHORT")] if stop else [])
        + ([_order("st", "TAKE_PROFIT_MARKET", D(take), "SHORT")] if take else [])
    )


async def test_short_breakeven_stop_no_alert_in_profit_alert_near_stop(ctx) -> None:  # type: ignore[no-untyped-def]
    """SHORT вход 100, стоп в безубытке 99 (ниже входа), без журнала: база
    1% входа = 1, полоса 20% = 0.2 — уведомление при mark ≥ 98.8."""
    await _as_short(ctx, stop="99")
    assert await ctx.step("95") == []            # прибыль растёт — стоп всё дальше
    assert await ctx.step("98.5") == []          # до стопа 0.5 > 0.2
    [text] = await ctx.step("98.9")              # до стопа 0.1
    assert "SOL-USDT SHORT</b> приближается к стопу" in text and "осталось" in text
    assert await ctx.alerts() == [("SL", D(99))]


async def test_long_stop_above_entry_uses_r(ctx) -> None:  # type: ignore[no-untyped-def]
    """LONG стоп перенесён выше входа (101 при входе 100), в журнале исходный
    стоп 90 → 1R = 10, полоса 2: уведомление при mark ≤ 103."""
    async with ctx.db.session() as session:
        trade = await TradeJournal(TradeRepository(session)).open_trade(
            user_id=ctx.uid, symbol="SOL-USDT", side=TradeSide.LONG, entry_price=ENTRY,
            quantity=D(2), stop_loss=D(90), source=TradeSource.IMPORTED,
            account_mode=ExchangeKeyMode.DEMO,
            external_position_id="p1", external_fill_id="f1", fee=D("0.1"),
        )
        trade.initial_stop_loss = D(90)
    ctx.exchange.orders = [_order("s9", "STOP_MARKET", D(101))]
    assert await ctx.step("110") == []           # было: «пройдено 1000%»
    assert await ctx.step("103.5") == []
    [text] = await ctx.step("102.5")             # до стопа 1.5 = 0.15R
    assert "приближается к стопу" in text and "(0.15R)" in text


async def test_short_take_above_entry(ctx) -> None:  # type: ignore[no-untyped-def]
    """Тейк SHORT выше входа (101, с убыточной стороны): срабатывает при
    падении к 101; без журнала база max(1, 1% входа) = 1, полоса 0.2."""
    await _as_short(ctx, take="101")
    assert await ctx.step("102") == []           # было: «пройдено 200%»
    [text] = await ctx.step("101.1")
    assert "приближается к тейку" in text


async def test_fallback_one_percent_without_journal(ctx) -> None:  # type: ignore[no-untyped-def]
    """|вход − уровень| = 0.2 < 1% входа: база 1, полоса 0.2 (а не 0.04 —
    «только у самого уровня»)."""
    await _as_short(ctx, stop="99.8")
    assert await ctx.step("99.5") == []          # до стопа 0.3
    assert len(await ctx.step("99.65")) == 1     # до стопа 0.15


async def test_hysteresis_for_flipped_level(ctx) -> None:  # type: ignore[no-untyped-def]
    """Полоса 0.2, сброс — когда до стопа больше 0.2 + 20% базы = 0.4."""
    await _as_short(ctx, stop="99")
    assert len(await ctx.step("98.9")) == 1
    assert await ctx.step("98.7") == []          # до стопа 0.3 — отметка держится
    assert await ctx.alerts() == [("SL", D(99))]
    assert await ctx.step("98.5") == []          # 0.5 > 0.4 — сброс
    assert await ctx.alerts() == []
    assert len(await ctx.step("98.9")) == 1      # новый подход
