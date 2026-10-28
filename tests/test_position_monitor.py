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

    async def get_positions(self, *, max_attempts=None) -> list[Position]:  # type: ignore[no-untyped-def]
        self.calls.append("positions")
        return [self.position] if self.position else []

    async def get_open_orders(self, symbol=None, *, max_attempts=None) -> list[OpenOrder]:  # type: ignore[no-untyped-def]
        self.calls.append("open_orders")
        return list(self.orders)

    async def close(self) -> None: ...


class FakeMarket:
    def __init__(self) -> None:
        self.mark = ENTRY

    async def get_mark_prices(self, symbols: list[str]) -> dict[str, Decimal | None]:
        return dict.fromkeys(symbols, self.mark)

    async def get_symbols(self, *, max_attempts=None) -> list[SymbolInfo]:  # type: ignore[no-untyped-def]
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
    text = await _one(ctx, "92")                # 80%
    assert "SOL-USDT LONG</b> приближается к стопу" in text
    assert "стоп 90" in text and "осталось 2.17%" in text
    assert "PnL -16.00 USDT" in text and "R не определён" in text   # позиции нет в журнале
    assert await ctx.alerts() == [("SL", D(90))]


async def test_take_80_percent_and_short(ctx) -> None:  # type: ignore[no-untyped-def]
    text = await _one(ctx, "116")
    assert "приближается к тейку" in text and "PnL +32.00 USDT" in text
    ctx.exchange.position = replace(ctx.exchange.position, side=TradeSide.SHORT)
    ctx.exchange.orders = [_order("s2", "STOP_MARKET", D(110), "SHORT"),
                           _order("t2", "TAKE_PROFIT_MARKET", D(80), "SHORT")]
    text = await _one(ctx, "108")               # SHORT: 80% к стопу 110
    assert "SOL-USDT SHORT</b> приближается к стопу" in text


async def test_r_from_journal_initial_stop(ctx) -> None:  # type: ignore[no-untyped-def]
    async with ctx.db.session() as session:
        await TradeJournal(TradeRepository(session)).open_trade(
            user_id=ctx.uid, symbol="SOL-USDT", side=TradeSide.LONG, entry_price=ENTRY,
            quantity=D(2), stop_loss=D(90), source=TradeSource.IMPORTED,
            account_mode=ExchangeKeyMode.DEMO,
            external_position_id="p1", external_fill_id="f1", fee=D("0.1"),
        )
    text = await _one(ctx, "92")
    # 1R = 10 на единицу: осталось 2 → 0.20R; PnL −16 на 2 шт → −0.80R
    assert "(0.20R)" in text and "(-0.80R)" in text


async def test_disabled_and_threshold_per_user(ctx) -> None:  # type: ignore[no-untyped-def]
    await ctx.set_user(notifications={"sl_approaching": False}, tp_alert_percent=50)
    assert await ctx.step("91") == []            # стоп выключен
    text = await _one(ctx, "110")               # тейк: порог 50% — 110
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
    text = await _one(ctx, "91.4")              # 80% к 91 = 91.8 — пройдено
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



async def _one(ctx, mark: str) -> str:  # type: ignore[no-untyped-def]
    """Ровно одно уведомление на шаге — проверкой (assert), не распаковкой: мутант иначе падал бы
    ValueError, а это не «поймана» (прогонщик мутаций, 28.10)."""
    texts = await ctx.step(mark)
    assert len(texts) == 1, texts
    return texts[0]

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


async def test_short_stop_in_profit_no_alert(ctx) -> None:  # type: ignore[no-untyped-def]
    """К9: SHORT вход 100, стоп 99 — в прибыли (БУ без журнала 99.90): риска убытка нет —
    «приближается к стопу» не шлётся и у самого стопа (до К9 — слался при mark ≥ 98.8)."""
    await _as_short(ctx, stop="99")
    assert await ctx.step("95") == []
    assert await ctx.step("98.9") == []          # до стопа 0.1
    assert await ctx.step("99.5") == []          # уровень пройден
    assert await ctx.alerts() == []


async def test_short_stop_between_breakeven_and_entry_alerts(ctx) -> None:  # type: ignore[no-untyped-def]
    """SHORT стоп 99.95: ниже входа, но выше БУ 99.90 — сработав, даст малый убыток: алерт есть.
    Без журнала база 1% входа = 1, полоса 0.2 — уведомление при mark ≥ 99.75."""
    await _as_short(ctx, stop="99.95")
    assert await ctx.step("99.6") == []          # до стопа 0.35
    text = await _one(ctx, "99.8")              # до стопа 0.15
    assert "SOL-USDT SHORT</b> приближается к стопу" in text and "осталось" in text
    assert await ctx.alerts() == [("SL", D("99.95"))]


async def test_long_stop_above_entry_uses_r(ctx) -> None:  # type: ignore[no-untyped-def]
    """LONG стоп чуть выше входа, но ниже безубытка (100.05 при входе 100, БУ 100.11) — малый
    убыток, алерт есть; в журнале исходный стоп 90 → 1R = 10, полоса 2: уведомление при
    mark ≤ 102.05. Стоп 101 (в прибыли) — К9, без алерта (до К9 — при mark ≤ 103)."""
    async with ctx.db.session() as session:
        trade = await TradeJournal(TradeRepository(session)).open_trade(
            user_id=ctx.uid, symbol="SOL-USDT", side=TradeSide.LONG, entry_price=ENTRY,
            quantity=D(2), stop_loss=D(90), source=TradeSource.IMPORTED,
            account_mode=ExchangeKeyMode.DEMO,
            external_position_id="p1", external_fill_id="f1", fee=D("0.1"),
        )
        trade.initial_stop_loss = D(90)
    ctx.exchange.orders = [_order("s9", "STOP_MARKET", D(101))]
    assert await ctx.step("102.5") == []         # К9: стоп 101 в прибыли — без алерта
    ctx.exchange.orders = [_order("s8", "STOP_MARKET", D("100.05"))]
    assert await ctx.step("110") == []           # было: «пройдено 1000%»
    assert await ctx.step("102.5") == []         # до стопа 2.45 > 2
    text = await _one(ctx, "101.55")            # до стопа 1.5 = 0.15R
    assert "приближается к стопу" in text and "(0.15R)" in text


async def test_short_take_above_entry(ctx) -> None:  # type: ignore[no-untyped-def]
    """Тейк SHORT выше входа (101, с убыточной стороны): срабатывает при
    падении к 101; без журнала база max(1, 1% входа) = 1, полоса 0.2."""
    await _as_short(ctx, take="101")
    assert await ctx.step("102") == []           # было: «пройдено 200%»
    text = await _one(ctx, "101.1")
    assert "приближается к тейку" in text


async def test_fallback_one_percent_without_journal(ctx) -> None:  # type: ignore[no-untyped-def]
    """|вход − уровень| = 0.05 < 1% входа: база 1, полоса 0.2 (а не 0.01 —
    «только у самого уровня»). Стоп 99.95 — между БУ 99.90 и входом (К9: ниже БУ — без алерта)."""
    await _as_short(ctx, stop="99.95")
    assert await ctx.step("99.6") == []          # до стопа 0.35
    assert len(await ctx.step("99.8")) == 1      # до стопа 0.15


async def test_hysteresis_for_flipped_level(ctx) -> None:  # type: ignore[no-untyped-def]
    """Полоса 0.2, сброс — когда до стопа больше 0.2 + 20% базы = 0.4 (стоп 99.95 — между БУ и
    входом, К9)."""
    await _as_short(ctx, stop="99.95")
    assert len(await ctx.step("99.8")) == 1
    assert await ctx.step("99.65") == []         # до стопа 0.3 — отметка держится
    assert await ctx.alerts() == [("SL", D("99.95"))]
    assert await ctx.step("99.5") == []          # 0.45 > 0.4 — сброс
    assert await ctx.alerts() == []
    assert len(await ctx.step("99.8")) == 1      # новый подход



# --- К9: стоп в безубытке — без алерта; кнопка БУ — по порогу плана -----------------------


@pytest.mark.parametrize(("stop", "alerts"), [("100.11", 0), ("100.10", 1)])
async def test_long_stop_exactly_at_breakeven_no_alert(ctx, stop: str, alerts: int) -> None:  # type: ignore[no-untyped-def]
    """БУ LONG без журнала — 100·1.0005/0.9995 = 100.10005 → вверх 100.11. Стоп ровно на БУ — без
    алерта; на шаг ниже — малый убыток, алерт (база 1% = 1, полоса 0.2: mark ≤ 100.30)."""
    ctx.exchange.orders = [_order("sb", "STOP_MARKET", D(stop))]
    assert len(await ctx.step("100.2")) == alerts


@pytest.mark.parametrize(("stop", "alerts"), [("99.90", 0), ("99.91", 1)])
async def test_short_stop_exactly_at_breakeven_no_alert(ctx, stop: str, alerts: int) -> None:  # type: ignore[no-untyped-def]
    """БУ SHORT без журнала — 100·0.9995/1.0005 = 99.90005 → вниз 99.90."""
    await _as_short(ctx, stop=stop)
    assert len(await ctx.step("99.8")) == alerts


async def test_breakeven_uses_journal_entry_fee(ctx) -> None:  # type: ignore[no-untyped-def]
    """Безубыток — как у карточки: комиссия входа журнала 0.4 на 2 → 0.2 на единицу, БУ
    100.2/0.9995 = 100.2501 → 100.26. Стоп 100.20 ставкой был бы в безубытке (БУ 100.11),
    с журналом — ниже БУ: алерт есть."""
    async with ctx.db.session() as session:
        await TradeJournal(TradeRepository(session)).open_trade(
            user_id=ctx.uid, symbol="SOL-USDT", side=TradeSide.LONG, entry_price=ENTRY,
            quantity=D(2), stop_loss=D(90), source=TradeSource.IMPORTED,
            account_mode=ExchangeKeyMode.DEMO,
            external_position_id="p1", external_fill_id="f1", fee=D("0.4"),
        )
    ctx.exchange.orders = [_order("sj", "STOP_MARKET", D("100.20"))]
    assert len(await ctx.step("100.3")) == 1
    # Кнопка БУ — от того же безубытка: порог 100.26/0.999 = 100.3603 > mark 100.3 — кнопки нет
    # (ставкой БУ 100.11, порог 100.2102 — была бы).
    _, keyboard = ctx.bot.sent[-1]
    assert not any(b.callback_data.startswith("pa:be:")
                   for row in keyboard.inline_keyboard for b in row)


def _view(side: TradeSide) -> Any:
    from app.execution.position_view import PositionView

    p = Position(symbol="SOL-USDT", side=side, quantity=D(2), entry_price=ENTRY,
                 mark_price=ENTRY, leverage=10, unrealized_pnl=D(0), liquidation_price=D(50),
                 position_id="p1")
    return PositionView(position=p, stops=(), takes=(), trade=None)


def _has_be(view: Any, mark: str, journal_fee_unit: Decimal | None = None) -> bool:
    kb = monitor_module.alert_keyboard(view, D(mark), fee_rate=D("0.0005"),
                                       min_distance_percent=D("0.1"), precision=2,
                                       journal_fee_unit=journal_fee_unit)
    return any(b.callback_data.startswith("pa:be:") for row in kb.inline_keyboard for b in row)


@pytest.mark.parametrize(("side", "mark", "has"), [
    # LONG: БУ 100.11; старая формула — mark ≥ 100.11·1.001 = 100.21011, план — mark ≥
    # 100.11/0.999 = 100.210210… Полоса между ними: старая давала кнопку, карточка отказывала.
    (TradeSide.LONG, "100.21015", False), (TradeSide.LONG, "100.2102", False),
    (TradeSide.LONG, "100.21022", True), (TradeSide.LONG, "100.22", True),
    # SHORT: БУ 99.90; старая — mark ≤ 99.90·0.999 = 99.8001, план — mark ≤ 99.90/1.001 =
    # 99.800199… Полоса: старая кнопки не давала, карточка проходила.
    (TradeSide.SHORT, "99.80015", True), (TradeSide.SHORT, "99.8002", False),
    (TradeSide.SHORT, "99.80", True),
])
def test_breakeven_button_same_threshold_as_plan(side: TradeSide, mark: str, has: bool) -> None:
    from app.execution.position_actions import breakeven_threshold, stop_clear_of_mark

    assert _has_be(_view(side), mark) is has
    be = D("100.11") if side is TradeSide.LONG else D("99.90")
    # Кнопка есть ⇔ карточка «Стоп в безубыток» пройдёт проверку плана при этом mark.
    assert stop_clear_of_mark(be, D(mark), D("0.1"), side) is has
    # На шаге цены — ровно «mark ≥ X» (LONG) / «mark ≤ X» (SHORT) подсказки.
    x = breakeven_threshold(side, be, D("0.1"), 2)
    assert x == (D("100.22") if side is TradeSide.LONG else D("99.80"))
    step = D("0.01") if side is TradeSide.LONG else D("-0.01")
    assert _has_be(_view(side), str(x)) and not _has_be(_view(side), str(x - step))


@pytest.mark.parametrize(("side", "mark"), [(TradeSide.LONG, "100.21015"),
                                            (TradeSide.SHORT, "99.80015")])
def test_old_button_formula_disagreed_in_band(side: TradeSide, mark: str) -> None:
    """Полоса выше — правда расхождение: старая формула БУ·(1±m) решала наоборот."""
    be, room, m = (D("100.11") if side is TradeSide.LONG else D("99.90")), D("0.001"), D(mark)
    old = m >= be * (1 + room) if side is TradeSide.LONG else m <= be * (1 - room)
    assert old is not _has_be(_view(side), mark)



def test_breakeven_button_uses_journal_fee() -> None:
    """Комиссия входа журнала 0.2 на единицу: БУ 100.26, порог 100.3603 — при mark 100.30 кнопки
    нет (ставкой — БУ 100.11, была бы), при 100.37 — есть."""
    view = _view(TradeSide.LONG)
    assert _has_be(view, "100.30") and not _has_be(view, "100.30", D("0.2"))
    assert _has_be(view, "100.37", D("0.2"))
