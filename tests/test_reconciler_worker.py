"""Сверка журнала с биржей целиком (шаг 15.6): настоящий BingXClient на
MockTransport, который отдаёт живые ответы демо 27.09 (tests/fixtures), —
разбор, решение, запись в журнал, события, уведомления.

SOL #4 закрыта на бирже стопом, в журнале открыта; LINK #3 открыта с живыми
SL/TP. На коде до 15.6 сверки нет — SOL остаётся OPEN, и главный тест падает
на этом, а не на импорте (см. _run_reconciler)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.security import SecretCipher
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.database.models.reconciliation_event import ReconciliationEvent
from app.database.models.signal import SignalRecord
from app.database.models.signal_notification import SignalNotification
from app.database.models.trade import Trade
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.bingx import BingXClient
from app.services.user_service import UserService
from app.trading.enums import (
    CancelSource,
    ExchangeKeyMode,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionActionKind,
    PositionActionStatus,
    ReconciliationKind,
    SignalDirection,
    SignalLevel,
    TradeSide,
    TradeSource,
    TradeStatus,
)
from app.trading.exit_reasons import EXIT_STOP_LOSS
from app.trading.journal import TradeJournal
from tests.bingx_fixtures import LINK_MANUAL_STOP, live_items
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
SOL_ENTRY, SOL_STOP, SOL_TAKE = "2104213344135159808", "2104213344721920001", "2104213344721920000"
SOL_CHILD = "2104219661398712320"
LINK_ENTRY, LINK_STOP, LINK_TAKE = (
    "2104122757776154624", "2104122758140616705", "2104122758140616704",
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent.append(text)


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "", "data": data})


class LiveDemo:
    """Маршрутизатор MockTransport: живые ответы демо по пути и символу."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.link_open_orders: list[dict[str, Any]] = live_items("openOrders LINK")
        # A.3: openOrders прочих символов (по умолчанию — пусто).
        self.open_orders: dict[str, list[dict[str, Any]]] = {}
        self.unknown_cids: set[str] = set()
        # GET /trade/order по clientOrderID: cid → живой ответ ордера.
        self.orders_by_cid: dict[str, dict[str, Any]] = {}
        # Деплой 4: openOrders отвечает ошибкой биржи.
        self.fail_open_orders = False
        # По умолчанию — состояние 27.09 (LINK открыта, SOL закрыта стопом);
        # сценарий 29.09 подменяет позиции и историю LINK.
        self.positions: list[dict[str, Any]] = live_items("positions all")
        self.all_orders: dict[str, list[dict[str, Any]]] = {
            "SOL-USDT": live_items("allOrders SOL"),
        }
        # 24.10: окно allOrders как у биржи — по времени СОЗДАНИЯ (time)
        # ордера (разведка 23.10). Включается тестом: живые ответы 27.09
        # старше окна «сейчас − 7 дней».
        self.window_by_created = False
        self.all_orders_windows: list[tuple[str, int, int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        self.calls.append(path)
        if path == "/openApi/swap/v2/user/positions":
            return _ok(self.positions)
        if path == "/openApi/swap/v2/trade/allOrders":
            assert params["symbol"] in self.all_orders, "история нужна только закрытым"
            start, end = int(params["startTime"]), int(params["endTime"])
            self.all_orders_windows.append((params["symbol"], start, end))
            orders = self.all_orders[params["symbol"]]
            if self.window_by_created:
                orders = [o for o in orders if start <= int(o["time"]) <= end]
            return _ok({"orders": orders})
        if path == "/openApi/swap/v2/trade/openOrders" and self.fail_open_orders:
            return httpx.Response(200, json={"code": 109400, "msg": "rejected", "data": {}})
        if path == "/openApi/swap/v2/trade/openOrders":
            # Без символа — все ордера (этап 3: синхронизация уровней журнала).
            symbol = params.get("symbol")
            orders = (
                self.link_open_orders if symbol in ("LINK-USDT", None)
                else self.open_orders.get(symbol, [])
            )
            return _ok({"orders": orders})
        cid = params.get("clientOrderID")
        if path == "/openApi/swap/v2/trade/order" and cid in self.unknown_cids:
            return httpx.Response(200, json={"code": 109421, "msg": "order not exist", "data": {}})
        if path == "/openApi/swap/v2/trade/order" and cid in self.orders_by_cid:
            return _ok({"order": self.orders_by_cid[cid]})
        raise AssertionError(f"неожиданный запрос {path} {dict(params)}")

    def client(self) -> BingXClient:
        return BingXClient(
            api_key="k", api_secret="s",
            client=httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="https://test"),
        )


@pytest_asyncio.fixture
async def ctx(unique_telegram_id, monkeypatch):  # type: ignore[no-untyped-def]
    settings = Settings(bingx_trading_mode="demo")  # type: ignore[call-arg]
    db = Database(settings)
    demo = LiveDemo()

    class Factory:
        def __init__(self, settings, cipher) -> None:  # type: ignore[no-untyped-def]
            pass

        async def for_user(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
            return demo.client()

    # До 15.6 модуля нет — патчить нечего, тест падает на поведении.
    monkeypatch.setattr("app.services.exchange_factory.ExchangeFactory", Factory)
    try:
        import app.workers.reconciler as reconciler_module
    except ModuleNotFoundError:
        reconciler_module = None
    if reconciler_module is not None:
        monkeypatch.setattr(reconciler_module, "ExchangeFactory", Factory)
    async with db.session() as session:
        svc = UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        )
        user = await svc.get_or_create(telegram_id=unique_telegram_id())
        await session.commit()

        # В тестовой БД реальные входы есть и у пользователей других тестов —
        # сверка этого теста видит только своего (на проде пользователь один).
        async def only_this_user(self) -> list[int]:  # type: ignore[no-untyped-def]
            return [user.id]

        monkeypatch.setattr(
            "app.database.repositories.execution_order.ExecutionOrderRepository"
            ".users_with_real_entries",
            only_this_user,
            raising=False,
        )
        yield settings, db, session, user, demo
        await session.rollback()
        user = await UserRepository(session).get_by_id(user.id)
        await cleanup_user(session, user)
        await session.commit()
    await db.dispose()


async def _notification(session, user_id: int, symbol: str) -> SignalNotification:  # type: ignore[no-untyped-def]
    slot = SignalRecord(
        user_id=user_id, symbol=symbol, timeframe="1h", level=SignalLevel.READY,
        direction=SignalDirection.LONG, setup="Пробой с ретестом", fingerprint=f"fp-{symbol}",
        detail="d", expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    session.add(slot)
    await session.flush()
    n = SignalNotification.snapshot_of(
        slot, notified_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(hours=4)
    )
    session.add(n)
    await session.flush()
    return n


def _row(user_id: int, nid: int, symbol: str, role: OrderRole, status: OrderStatus,
         exchange_id: str | None, **kw: Any) -> ExecutionOrder:
    letter = {OrderRole.ENTRY: "E", OrderRole.STOP_LOSS: "S", OrderRole.TAKE_PROFIT: "T"}[role]
    order_type = {
        OrderRole.ENTRY: OrderType.MARKET, OrderRole.STOP_LOSS: OrderType.STOP_MARKET,
        OrderRole.TAKE_PROFIT: OrderType.TAKE_PROFIT_MARKET,
    }[role]
    return ExecutionOrder(
        account_mode=ExchangeKeyMode.DEMO,
        user_id=user_id, notification_id=nid, client_order_id=f"tj{nid}u{user_id}{letter}",
        exchange_order_id=exchange_id, symbol=symbol,
        side=OrderSide.BUY if role is OrderRole.ENTRY else OrderSide.SELL,
        position_side=TradeSide.LONG, order_type=order_type, role=role, status=status, **kw,
    )


async def _bot_trade(  # type: ignore[no-untyped-def]
    session, user_id: int, symbol: str, *, entry: str, qty: str, entry_id: str,
    stop_id: str, take_id: str, opened_at: datetime, fee: str, risk_amount: str | None = None,
) -> Trade:
    n = await _notification(session, user_id, symbol)
    trade = await TradeJournal(TradeRepository(session)).open_trade(
        user_id=user_id, symbol=symbol, side=TradeSide.LONG, entry_price=D(entry),
        quantity=D(qty), leverage=10, opened_at=opened_at, source=TradeSource.SIGNAL_EXECUTION,
        account_mode=ExchangeKeyMode.DEMO,
        notification_id=n.id, external_fill_id=entry_id, fee=D(fee),
    )
    session.add_all([
        _row(user_id, n.id, symbol, OrderRole.ENTRY, OrderStatus.FILLED, entry_id,
             trade_id=trade.id, risk_amount=D(risk_amount) if risk_amount else None),
        _row(user_id, n.id, symbol, OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, stop_id),
        _row(user_id, n.id, symbol, OrderRole.TAKE_PROFIT, OrderStatus.SUBMITTED, take_id),
    ])
    await session.commit()
    return trade


async def _seed_live(session, user_id: int, sol_entry: str = "123.021") -> tuple[int, int]:  # type: ignore[no-untyped-def]
    # risk_amount — живой риск SOL #4 с карточки: 1R для сверки PnL (28.09).
    sol = await _bot_trade(
        session, user_id, "SOL-USDT", entry=sol_entry, qty="1362.07", entry_id=SOL_ENTRY,
        stop_id=SOL_STOP, take_id=SOL_TAKE, fee="83.781520", risk_amount="1766.609542",
        opened_at=datetime(2026, 9, 27, 14, 15, 30, 300000, tzinfo=UTC),
    )
    link = await _bot_trade(
        session, user_id, "LINK-USDT", entry="14.4", qty="2037.8", entry_id=LINK_ENTRY,
        stop_id=LINK_STOP, take_id=LINK_TAKE, fee="14.672461",
        opened_at=datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC),
    )
    return sol.id, link.id


async def _run_reconciler(settings, db, bot, redis=None) -> None:  # type: ignore[no-untyped-def]
    """На коде до 15.6 модуля нет — сверка не выполняется вовсе, и тесты
    падают на поведении (SOL остаётся OPEN), а не на ImportError."""
    try:
        from app.workers.reconciler import Reconciler
    except ModuleNotFoundError:
        return
    cipher = SecretCipher(settings.encryption_key.get_secret_value())
    await Reconciler(bot, db, settings, cipher, redis).run()


async def _trade(db, trade_id: int) -> Trade:  # type: ignore[no-untyped-def]
    async with db.session() as s:
        trade = await s.scalar(
            select(Trade).where(Trade.id == trade_id).options(selectinload(Trade.fills))
        )
        assert trade is not None
        return trade


async def _rows(db, symbol: str) -> dict[OrderRole, ExecutionOrder]:  # type: ignore[no-untyped-def]
    async with db.session() as s:
        rows = await s.scalars(select(ExecutionOrder).where(ExecutionOrder.symbol == symbol))
        return {r.role: r for r in rows}


async def test_sol_closed_by_stop_on_exchange_is_closed_in_journal(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, _demo = ctx
    sol_id, link_id = await _seed_live(session, user.id)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    sol = await _trade(db, sol_id)
    assert sol.status is TradeStatus.CLOSED
    assert sol.exit_price == D("121.611")
    assert sol.fees == D("166.602903")  # 83.78152 вход + 82.821383 выход — как у биржи
    assert sol.exit_reason == EXIT_STOP_LOSS
    assert sol.closed_at == datetime(2026, 9, 27, 14, 40, 36, tzinfo=UTC)
    assert {f.external_fill_id for f in sol.fills} == {SOL_ENTRY, SOL_CHILD}
    rows = await _rows(db, "SOL-USDT")
    assert rows[OrderRole.STOP_LOSS].status is OrderStatus.FILLED
    assert rows[OrderRole.STOP_LOSS].cancel_source is None
    # A.3: тейк снят биржей при закрытии (CANCELLED в истории SOL) — единое
    # CANCELLED и источник EXCHANGE.
    assert rows[OrderRole.TAKE_PROFIT].status is OrderStatus.CANCELLED
    assert rows[OrderRole.TAKE_PROFIT].cancel_source is CancelSource.EXCHANGE

    link = await _trade(db, link_id)
    assert link.status is TradeStatus.OPEN
    assert len(bot.sent) == 1 and "закрыта по стопу" in bot.sent[0]
    assert "#" + str(sol_id) in bot.sent[0]


@pytest.mark.parametrize("field", ["avgPrice", "commission", "profit"])
async def test_filled_exit_with_empty_number_is_not_written(ctx, field: str) -> None:  # type: ignore[no-untyped-def]
    """Разведка 29.09: у исполненного выхода пустая цифра. Раньше "" → 0:
    commission — выход записан с комиссией 0 (PnL завышен, сверка PnL не
    видит — комиссии нет с обеих сторон); profit — 0 в trade_fills и ложный
    PNL_MISMATCH; avgPrice — цена 0, JournalError и молчаливый WARNING каждый
    цикл. Теперь чтение allOrders — ошибка разбора: журнал не тронут, ошибка
    в пульсе. СИНТЕТИКА ИЗ ЖИВОГО allOrders SOL, у стопа-выхода поле ""."""
    settings, db, session, user, demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id)
    orders = live_items("allOrders SOL")
    [child] = [o for o in orders if str(o["orderId"]) == SOL_CHILD]
    child[field] = ""
    demo.all_orders["SOL-USDT"] = orders
    bot = FakeBot()
    from app.workers.reconciler import Reconciler

    reconciler = Reconciler(
        bot, db, settings, SecretCipher(settings.encryption_key.get_secret_value())
    )
    await reconciler.run()

    sol = await _trade(db, sol_id)
    assert sol.status is TradeStatus.OPEN
    assert {f.external_fill_id for f in sol.fills} == {SOL_ENTRY}
    assert reconciler.pulse.window(datetime.now(UTC)).errors == 1
    assert bot.sent == []


async def test_sol_stop_notification_text(ctx) -> None:  # type: ignore[no-untyped-def]
    """Живой стоп SOL #4 (27.09 14:40:36 UTC): тот же формат, что у закрытия
    вне бота — «Закрыта: DD.MM HH:MM» в поясе пользователя, суммы
    форматтерами. Раньше — fmt_decimal («82.821383») и без времени."""
    settings, db, session, user, _demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    [text] = bot.sent
    assert text.splitlines() == [
        "🛑 SOL-USDT LONG закрыта по стопу на бирже",
        "Закрыта: 27.09 19:40",
        "Выход: 121.611 · объём 1362.07",
        "Комиссия выхода: 82.82 VST",
        "PnL: -2087.12 VST · комиссии вход+выход 166.60 VST",  # -2087.121603
        f"📒 Сделка #{sol_id} закрыта в журнале",
    ]


async def test_sol_stop_exit_price_to_symbol_step(ctx) -> None:  # type: ignore[no-untyped-def]
    """К4 (28.10.2026): справочник инструментов есть (общий кэш процесса) — цена выхода в
    уведомлении до шага символа («121.6110» при точности 4), путь сверки целиком."""
    from app.exchanges.base import SymbolInfo
    from app.market.cache import market_cache
    from app.market.data import symbols_key

    settings, db, session, user, _demo = ctx
    await _seed_live(session, user.id)
    bot = FakeBot()
    market_cache.set(symbols_key("bingx"), [SymbolInfo("SOL-USDT", 4, 2, D(1), D(5))], 60)
    try:
        await _run_reconciler(settings, db, bot)
    finally:
        market_cache.invalidate(symbols_key("bingx"))

    [text] = bot.sent
    assert "Выход: 121.6110 · объём 1362.07" in text.splitlines()


async def test_second_run_writes_and_notifies_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, _demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id)
    bot = FakeBot()
    await _run_reconciler(settings, db, bot)
    assert len(bot.sent) == 1

    await _run_reconciler(settings, db, bot)

    assert len(bot.sent) == 1
    sol = await _trade(db, sol_id)
    assert len(sol.fills) == 2  # вход + один выход, второй раз не записан


async def test_live_confirm_lock_skips_whole_cycle(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    sol_id, _ = await _seed_live(session, user.id)
    redis = FakeRedis(decode_responses=True)
    await redis.set(f"exec:lock:{user.id}:n999", "x", ex=60)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot, redis)

    assert demo.calls == []
    assert (await _trade(db, sol_id)).status is TradeStatus.OPEN
    await redis.aclose()


async def test_missing_stop_alarm_once(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    settings.reconciler_stop_check_every = 1
    await _seed_live(session, user.id)
    demo.link_open_orders = []
    # п.1а: строк стопа/тейка нет в openOrders при живой позиции — сверка
    # перечитывает историю символа (здесь их нет и в ней).
    demo.all_orders["LINK-USDT"] = []
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)
    await _run_reconciler(settings, db, bot)

    alarms = [t for t in bot.sent if t.startswith("⚠️ ПОЗИЦИЯ БЕЗ СТОПА: LINK-USDT")]
    assert len(alarms) == 1


async def test_unknown_entry_not_found_after_window_is_not_placed(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    n = await _notification(session, user.id, "ADA-USDT")
    trade = await TradeJournal(TradeRepository(session)).open_trade(
        user_id=user.id, symbol="ADA-USDT", side=TradeSide.LONG, entry_price=D("1"),
        quantity=D("10"), source=TradeSource.SIGNAL_EXECUTION, notification_id=n.id,
        account_mode=ExchangeKeyMode.DEMO,
        fill_confirmed=False,
    )
    entry = _row(user.id, n.id, "ADA-USDT", OrderRole.ENTRY, OrderStatus.UNKNOWN, None,
                 trade_id=trade.id, created_at=datetime.now(UTC) - timedelta(minutes=11))
    session.add(entry)
    await session.commit()
    demo.unknown_cids.add(entry.client_order_id)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    rows = await _rows(db, "ADA-USDT")
    assert rows[OrderRole.ENTRY].status is OrderStatus.NOT_PLACED
    assert (await _trade(db, trade.id)).status is TradeStatus.CANCELLED
    assert any("не выставлен на бирже" in t for t in bot.sent)


async def test_unknown_entry_found_new_live_form_is_ambiguous(ctx) -> None:  # type: ignore[no-untyped-def]
    """Р2: поиск нашёл вход в статусе NEW (живая форма #38 — пустая
    commission) — расхождение AMBIGUOUS «найден в статусе NEW», журнал не
    правится. Раньше разбор падал ReadbackIncomplete, и reconciler молча
    повторял поиск каждый цикл."""
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    n = await _notification(session, user.id, "ADA-USDT")
    entry = _row(user.id, n.id, "ADA-USDT", OrderRole.ENTRY, OrderStatus.UNKNOWN, None,
                 created_at=datetime.now(UTC) - timedelta(minutes=11))
    session.add(entry)
    await session.commit()
    [demo.orders_by_cid[entry.client_order_id]] = live_items("order #38 LINK-USDT STOP_LOSS")
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    assert (await _rows(db, "ADA-USDT"))[OrderRole.ENTRY].status is OrderStatus.UNKNOWN
    [event] = [e for e in await _events(db, user.id) if e.dedup_key == f"entry:{entry.id}"]
    assert event.kind is ReconciliationKind.AMBIGUOUS and event.resolved_at is None
    assert "найден в статусе NEW" in event.detail
    assert sum("найден в статусе NEW" in t for t in bot.sent) == 1


async def test_unknown_entry_inside_window_untouched(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    n = await _notification(session, user.id, "ADA-USDT")
    entry = _row(user.id, n.id, "ADA-USDT", OrderRole.ENTRY, OrderStatus.UNKNOWN, None,
                 created_at=datetime.now(UTC) - timedelta(minutes=5))
    session.add(entry)
    await session.commit()
    demo.unknown_cids.add(entry.client_order_id)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    assert (await _rows(db, "ADA-USDT"))[OrderRole.ENTRY].status is OrderStatus.UNKNOWN
    assert "/openApi/swap/v2/trade/order" not in demo.calls


# ---------------------------------------------------------------------------
# 28.09, блок C: PnL журнала против биржи при полном закрытии
# ---------------------------------------------------------------------------


async def _events(db, user_id: int) -> list[ReconciliationEvent]:  # type: ignore[no-untyped-def]
    async with db.session() as s:
        stmt = select(ReconciliationEvent).where(ReconciliationEvent.user_id == user_id)
        return list(await s.scalars(stmt))


async def test_sol_exit_keeps_exchange_profit_and_pnl_matches(ctx) -> None:  # type: ignore[no-untyped-def]
    """Живой SOL #4: profit биржи −1920.2749, минус комиссии 166.602903 —
    −2086.877803; журнал −2087.121603. Разница 0.24 < 0.01R (17.67) —
    расхождения нет, лишнего уведомления нет."""
    settings, db, session, user, _demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    sol = await _trade(db, sol_id)
    assert sol.pnl == D("-2087.121603")
    [exit_fill] = [f for f in sol.fills if f.external_fill_id == SOL_CHILD]
    assert exit_fill.exchange_realized_pnl == D("-1920.2749")
    [entry_fill] = [f for f in sol.fills if f.external_fill_id == SOL_ENTRY]
    assert entry_fill.exchange_realized_pnl is None
    assert not [e for e in await _events(db, user.id) if e.kind is ReconciliationKind.PNL_MISMATCH]
    assert len(bot.sent) == 1


async def test_pnl_mismatch_is_anomaly_notified_once(ctx) -> None:  # type: ignore[no-untyped-def]
    """Вход в журнале 123.000 вместо биржевых 123.021: PnL журнала на 28.36
    выше биржевого — больше 0.01R. Журнал не правится, событие
    PNL_MISMATCH сразу разрешено (сделка закрыта), уведомление одно."""
    settings, db, session, user, _demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id, sol_entry="123.000")
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    sol = await _trade(db, sol_id)
    assert sol.status is TradeStatus.CLOSED
    assert sol.pnl == D("-2058.518133")  # журнал не правится
    [event] = [e for e in await _events(db, user.id) if e.kind is ReconciliationKind.PNL_MISMATCH]
    assert event.trade_id == sol_id
    assert event.dedup_key == f"pnl:{sol_id}"
    assert event.resolved_at is not None and event.notified_at is not None
    assert "PnL журнала -2058.518133, по бирже -2086.877803" in event.detail
    assert len(bot.sent) == 2
    assert "Сверка с биржей, SOL-USDT: PnL журнала" in bot.sent[1]

    await _run_reconciler(settings, db, bot)
    assert len(bot.sent) == 2


async def _drop_entry_risk(session, trade_id: int, stop_loss: str | None) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import update

    await session.execute(
        update(ExecutionOrder)
        .where(ExecutionOrder.trade_id == trade_id, ExecutionOrder.role == OrderRole.ENTRY)
        .values(risk_amount=None)
    )
    await session.execute(
        update(Trade).where(Trade.id == trade_id)
        .values(stop_loss=D(stop_loss) if stop_loss else None)
    )
    await session.commit()


async def test_pnl_1r_falls_back_to_stop_distance(ctx) -> None:  # type: ignore[no-untyped-def]
    """Нет risk_amount у ENTRY — 1R = |вход − стоп| × объём входа:
    |123.000 − 121.646| × 1362.07 = 1844.28, допуск 18.44 < 28.36."""
    settings, db, session, user, _demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id, sol_entry="123.000")
    await _drop_entry_risk(session, sol_id, "121.646")
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    [event] = [e for e in await _events(db, user.id) if e.kind is ReconciliationKind.PNL_MISMATCH]
    assert "больше 0.01R (18.44" in event.detail


async def test_lock_pnl_not_checked_without_1r(ctx) -> None:  # type: ignore[no-untyped-def]
    """ЗАМОК: ни risk_amount, ни стопа — сверять не с чем, это не расхождение.

    Отрицательный тест: на коде до 28.09 падает только из-за отсутствия
    ReconciliationKind.PNL_MISMATCH (AttributeError), не по поведению —
    старый код аномалию тоже не создаёт. Держит сверку от ложной тревоги,
    если 1R неизвестен; удалять как «проходящий и без правки» нельзя."""
    settings, db, session, user, _demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id, sol_entry="123.000")
    await _drop_entry_risk(session, sol_id, None)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    assert (await _trade(db, sol_id)).status is TradeStatus.CLOSED
    assert not [e for e in await _events(db, user.id) if e.kind is ReconciliationKind.PNL_MISMATCH]
    assert len(bot.sent) == 1


# --- 28.09: уведомления сверки «хотя бы один раз» -------------------------------


class NetworkFailBot(FakeBot):
    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        from aiogram.exceptions import TelegramNetworkError

        raise TelegramNetworkError(method=None, message="Request timeout error")  # type: ignore[arg-type]


class ForbiddenBot(FakeBot):
    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        from aiogram.exceptions import TelegramForbiddenError

        self.sent.append(text)
        raise TelegramForbiddenError(method=None, message="forbidden")  # type: ignore[arg-type]


async def _pending_event(  # type: ignore[no-untyped-def]
    session, user_id: int, *, age: timedelta, last_attempt_age: timedelta | None = None,
    text: str = "🛑 SOL-USDT LONG закрыта по стопу на бирже",
) -> int:
    """Событие-факт, уведомление о котором не ушло: notified_at IS NULL."""
    now = datetime.now(UTC)
    event = ReconciliationEvent(
        user_id=user_id, symbol="SOL-USDT", kind=ReconciliationKind.CLOSED_STOP_LOSS,
        dedup_key=f"close:test:{age}", detail="Стоп-лосс на бирже", created_at=now - age,
        resolved_at=now - age, notify_text=text, attempts=1,
        last_attempt_at=(now - last_attempt_age) if last_attempt_age is not None else now - age,
    )
    session.add(event)
    await session.commit()
    return event.id


async def _event(db, event_id: int) -> ReconciliationEvent:  # type: ignore[no-untyped-def]
    async with db.session() as s:
        event = await s.get(ReconciliationEvent, event_id)
        assert event is not None
        return event


class _DownFactory:
    """Биржа недоступна: сверка пользователя падает на for_user."""

    def __init__(self, settings, cipher) -> None:  # type: ignore[no-untyped-def]
        pass

    async def for_user(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
        from app.exchanges.base import ExchangeUnavailableError

        raise ExchangeUnavailableError("BingX недоступен")


async def test_failed_close_notification_is_resent_next_cycle(ctx) -> None:  # type: ignore[no-untyped-def]
    """SOL #4 закрыта стопом, Telegram лёг на уведомлении: событие остаётся с
    notified_at NULL и текстом; следующий цикл отправляет его. Раньше повтора
    не было — закрытие терялось."""
    settings, db, session, user, _demo = ctx
    await _seed_live(session, user.id)

    await _run_reconciler(settings, db, NetworkFailBot())
    events = await _events(db, user.id)
    [event] = [e for e in events if e.kind is ReconciliationKind.CLOSED_STOP_LOSS]
    assert event.notified_at is None
    assert event.attempts == 1
    assert event.notify_text is not None and "закрыта по стопу" in event.notify_text

    bot = FakeBot()
    await _run_reconciler(settings, db, bot)
    assert [t for t in bot.sent if "закрыта по стопу" in t] == [event.notify_text]
    resent = await _event(db, event.id)
    assert resent.notified_at is not None
    assert resent.attempts == 2


async def test_resend_works_while_exchange_is_down(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.workers.reconciler as reconciler_module

    settings, db, session, user, _demo = ctx
    event_id = await _pending_event(session, user.id, age=timedelta(minutes=1))
    monkeypatch.setattr(reconciler_module, "ExchangeFactory", _DownFactory)

    bot = FakeBot()
    await _run_reconciler(settings, db, bot)

    assert bot.sent == ["🛑 SOL-USDT LONG закрыта по стопу на бирже"]
    assert (await _event(db, event_id)).notified_at is not None


async def test_late_resend_has_event_time_in_user_tz(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.workers.reconciler as reconciler_module
    from app.trading.risk import tz_offset_for

    settings, db, session, user, _demo = ctx
    event_id = await _pending_event(session, user.id, age=timedelta(minutes=10))
    created = (await _event(db, event_id)).created_at
    monkeypatch.setattr(reconciler_module, "ExchangeFactory", _DownFactory)

    bot = FakeBot()
    await _run_reconciler(settings, db, bot)

    local = created + timedelta(hours=tz_offset_for(user.settings.timezone))
    assert bot.sent == [
        f"⏱ Событие от {local:%H:%M} (доставлено с опозданием)\n"
        "🛑 SOL-USDT LONG закрыта по стопу на бирже"
    ]


async def test_slow_mode_error_once_then_every_ten_minutes(ctx, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    import app.workers.reconciler as reconciler_module

    settings, db, session, user, _demo = ctx
    event_id = await _pending_event(
        session, user.id, age=timedelta(minutes=31), last_attempt_age=timedelta(minutes=2)
    )
    monkeypatch.setattr(reconciler_module, "ExchangeFactory", _DownFactory)

    with caplog.at_level(logging.WARNING, logger="app.workers.reconciler"):
        await _run_reconciler(settings, db, NetworkFailBot())
        await _run_reconciler(settings, db, NetworkFailBot())

    errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "за 30 мин" in r.getMessage()
    ]
    assert len(errors) == 1
    # Первая попытка редкого режима — сразу, вторая — не раньше чем через 10 мин.
    assert (await _event(db, event_id)).attempts == 2


async def test_older_than_max_age_is_given_up_with_warning(ctx, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    import app.workers.reconciler as reconciler_module

    settings, db, session, user, _demo = ctx
    event_id = await _pending_event(session, user.id, age=timedelta(hours=25))
    monkeypatch.setattr(reconciler_module, "ExchangeFactory", _DownFactory)

    bot = FakeBot()
    with caplog.at_level(logging.WARNING, logger="app.workers.reconciler"):
        await _run_reconciler(settings, db, bot)

    assert bot.sent == []
    event = await _event(db, event_id)
    assert event.gave_up_at is not None and event.notified_at is None
    assert [r for r in caplog.records if r.levelno == logging.WARNING and "отказ" in r.getMessage()]


async def test_forbidden_gives_up_without_retry(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.workers.reconciler as reconciler_module

    settings, db, session, user, _demo = ctx
    event_id = await _pending_event(session, user.id, age=timedelta(minutes=1))
    monkeypatch.setattr(reconciler_module, "ExchangeFactory", _DownFactory)

    forbidden = ForbiddenBot()
    await _run_reconciler(settings, db, forbidden)
    assert (await _event(db, event_id)).gave_up_at is not None

    bot = FakeBot()
    await _run_reconciler(settings, db, bot)
    assert len(forbidden.sent) == 1
    assert bot.sent == []


# --- 28.09: пульс reconciler ----------------------------------------------------


async def test_pulse_counts_lock_skip_and_exchange_error(ctx, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """Пропуск по локу и сбой биржи — в пульсе; INFO после N запусков."""
    import logging

    import app.workers.reconciler as reconciler_module

    settings, db, _session, _user, _demo = ctx
    settings.reconciler_pulse_every = 2
    monkeypatch.setattr(reconciler_module, "ExchangeFactory", _DownFactory)
    redis = FakeRedis()
    await redis.set("exec:lock:1:1", "1", ex=60)
    cipher = SecretCipher(settings.encryption_key.get_secret_value())
    reconciler = reconciler_module.Reconciler(FakeBot(), db, settings, cipher, redis)

    with caplog.at_level(logging.INFO, logger="app.workers.reconciler"):
        await reconciler.run()  # лок жив — пропуск
        await redis.delete("exec:lock:1:1")
        await reconciler.run()  # биржа лежит — ошибка сверки пользователя

    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Пульс")]
    assert line.startswith(
        "Пульс reconciler: циклов 1 за 1 мин, пропущено по локу 1, ошибок 1, "
        "событий 0, переотправлено 0, последний "
    )
    window = reconciler.pulse.window(datetime.now(UTC))
    assert (window.cycles, window.errors, window.since_start) == (1, 1, True)


# --- 29.09: LINK #3 закрыта ручным стопом владельца --------------------------

LINK_MANUAL_CHILD = "2104784078754553856"
MANUAL_STOP_REASON = "Стоп, изменённый вручную — закрыто вне бота"


async def _seed_link_manual_stop(session, demo, user_id: int) -> Trade:  # type: ignore[no-untyped-def]
    """Состояние прода на 29.09 09:04 Екб: #3 открыта в журнале, позиции на
    бирже нет, история LINK — живой ответ 29.09, AMBIGUOUS #2 доставлен и
    не разрешён. 1R — риск карточки: (14.4 − 13.526) × 2037.8."""
    demo.positions = live_items("positions LINK (фильтр по символу на клиенте)", LINK_MANUAL_STOP)
    demo.all_orders = {"LINK-USDT": live_items("allOrders LINK", LINK_MANUAL_STOP)}
    # Позиции нет — её стоп и тейк биржа сняла (openOrders 27.09 их ещё отдаёт).
    demo.link_open_orders = []
    link = await _bot_trade(
        session, user_id, "LINK-USDT", entry="14.4", qty="2037.8", entry_id=LINK_ENTRY,
        stop_id=LINK_STOP, take_id=LINK_TAKE, fee="14.672461",
        risk_amount=str((D("14.4") - D("13.526")) * D("2037.8")),
        opened_at=datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC),
    )
    session.add(ReconciliationEvent(
        user_id=user_id, trade_id=link.id, symbol="LINK-USDT",
        kind=ReconciliationKind.AMBIGUOUS, dedup_key=f"ambiguous:{link.id}:{LINK_MANUAL_CHILD}",
        detail="закрывающий ордер не узнан",
        notified_at=datetime(2026, 9, 29, 4, 4, 11, tzinfo=UTC),
    ))
    await session.commit()
    return link


async def _trade_events(db, trade_id: int) -> list[ReconciliationEvent]:  # type: ignore[no-untyped-def]
    async with db.session() as s:
        rows = await s.scalars(
            select(ReconciliationEvent)
            .where(ReconciliationEvent.trade_id == trade_id)
            .order_by(ReconciliationEvent.id)
        )
        return list(rows)


async def test_link_manual_stop_closes_trade_from_fact(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    link = await _seed_link_manual_stop(session, demo, user.id)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    trade = await _trade(db, link.id)
    assert trade.status is TradeStatus.CLOSED
    assert trade.exit_price == D("14.776")
    assert trade.fees == D("29.727847")  # 14.672461 вход + 15.055386 выход
    # (14.776 − 14.4) × 2037.8 − 29.727847
    assert trade.pnl == (D("14.776") - D("14.4")) * D("2037.8") - D("29.727847")
    assert trade.exit_reason == MANUAL_STOP_REASON
    assert trade.closed_at == datetime(2026, 9, 29, 4, 3, 24, tzinfo=UTC)
    assert {f.external_fill_id for f in trade.fills} == {LINK_ENTRY, LINK_MANUAL_CHILD}
    rows = await _rows(db, "LINK-USDT")
    assert rows[OrderRole.STOP_LOSS].status is OrderStatus.CANCELLED
    assert rows[OrderRole.TAKE_PROFIT].status is OrderStatus.CANCELLED
    assert rows[OrderRole.STOP_LOSS].cancel_source is CancelSource.EXCHANGE
    assert rows[OrderRole.TAKE_PROFIT].cancel_source is CancelSource.EXCHANGE

    events = await _trade_events(db, link.id)
    ambiguous = [e for e in events if e.kind is ReconciliationKind.AMBIGUOUS]
    assert len(ambiguous) == 1 and ambiguous[0].resolved_at is not None
    [closed] = [e for e in events if e.kind is ReconciliationKind.CLOSED_OUTSIDE_BOT]
    assert closed.dedup_key == f"close:{link.id}:{LINK_MANUAL_CHILD}"
    assert closed.notified_at is not None
    assert not [e for e in events if e.kind is ReconciliationKind.PNL_MISMATCH]
    assert len(bot.sent) == 1


async def test_link_manual_stop_notification_text(ctx) -> None:  # type: ignore[no-untyped-def]
    """Время исполнения в поясе пользователя (Екб, +5), суммы — форматтерами;
    валюта демо — VST. Вне бота — ℹ️ и короткая причина; полный exit_reason
    — в журнале (test_link_manual_stop_closes_trade_from_fact)."""
    settings, db, session, user, demo = ctx
    link = await _seed_link_manual_stop(session, demo, user.id)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    [text] = bot.sent
    assert text.splitlines() == [
        "ℹ️ LINK-USDT LONG закрыта на бирже вне бота",
        "Стоп, изменённый вручную",
        "Закрыта: 29.09 09:03",
        "Выход: 14.776 · объём 2037.8",
        "Комиссия выхода: 15.06 VST",
        "PnL: +736.48 VST · комиссии вход+выход 29.73 VST",  # 736.484953
        f"📒 Сделка #{link.id} закрыта в журнале",
    ]


async def test_lock_link_manual_stop_second_run_is_silent(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    link = await _seed_link_manual_stop(session, demo, user.id)
    bot = FakeBot()
    await _run_reconciler(settings, db, bot)
    sent = len(bot.sent)

    await _run_reconciler(settings, db, bot)

    assert len(bot.sent) == sent
    trade = await _trade(db, link.id)
    assert len(trade.fills) == len({f.external_fill_id for f in trade.fills})


# ---------------------------------------------------------------------------
# п.8, 29.09: префикс dedup_key — до двоеточия, entry:1 не закрывает entry:12
# ---------------------------------------------------------------------------


def _foreign_open_event(user_id: int, kind: ReconciliationKind, key: str) -> ReconciliationEvent:
    """Открытое расхождение чужой записи с ключом, который начинается с ключа
    нашей (entry:{id} → entry:{id}2). Уже доставлено — не переотправляется."""
    return ReconciliationEvent(
        user_id=user_id, symbol="XRP-USDT", kind=kind, dedup_key=key, detail="чужое",
        notify_text="чужое", notified_at=datetime.now(UTC),
    )


async def test_entry_prefix_does_not_resolve_longer_entry_id(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    n = await _notification(session, user.id, "ADA-USDT")
    entry = _row(user.id, n.id, "ADA-USDT", OrderRole.ENTRY, OrderStatus.UNKNOWN, None,
                 created_at=datetime.now(UTC) - timedelta(minutes=11))
    session.add(entry)
    await session.flush()
    foreign_key = f"entry:{entry.id}2"
    session.add(_foreign_open_event(user.id, ReconciliationKind.AMBIGUOUS, foreign_key))
    await session.commit()
    demo.unknown_cids.add(entry.client_order_id)

    await _run_reconciler(settings, db, FakeBot())

    assert (await _rows(db, "ADA-USDT"))[OrderRole.ENTRY].status is OrderStatus.NOT_PLACED
    [foreign] = [e for e in await _events(db, user.id) if e.dedup_key == foreign_key]
    assert foreign.resolved_at is None


async def test_qty_and_stop_missing_prefix_do_not_resolve_longer_trade_id(ctx) -> None:  # type: ignore[no-untyped-def]
    """LINK открыта и совпадает с биржей — сверка разрешает свои qty:{id} и
    stop_missing:{id}, но не qty:{id}2 / stop_missing:{id}2."""
    settings, db, session, user, _demo = ctx
    settings.reconciler_stop_check_every = 1
    _, link_id = await _seed_live(session, user.id)
    keys = {
        ReconciliationKind.QUANTITY_MISMATCH: f"qty:{link_id}2",
        ReconciliationKind.STOP_MISSING: f"stop_missing:{link_id}2",
    }
    session.add_all(_foreign_open_event(user.id, kind, key) for kind, key in keys.items())
    await session.commit()

    await _run_reconciler(settings, db, FakeBot())

    by_key = {e.dedup_key: e for e in await _events(db, user.id)}
    assert all(by_key[key].resolved_at is None for key in keys.values())


# ---------------------------------------------------------------------------
# _confirm_entry и путь к нему (_resolve_unresolved_entry), 29.09.
# Вход LINK #3 с неизвестным исходом старше окна (10 мин), GET по
# clientOrderID отдаёт живой #37 (демо 27.09): 2037.8 по 14.400, комиссия
# 14.672461, updateTime 08:15:32.828, positionID …754. Живая позиция LINK в
# positions есть. Замены полей #37 помечены в тесте.
# ---------------------------------------------------------------------------

LINK_POSITION = "2104122757805514754"
LINK_FILLED_AT = datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC)
ORDER_GET = "/openApi/swap/v2/trade/order"


def _live_entry(**overrides: Any) -> dict[str, Any]:
    """Живой GET #37; overrides — синтетика из живого (None — поля нет)."""
    [raw] = live_items("order #37 LINK-USDT ENTRY")
    raw.update(overrides)
    return {k: v for k, v in raw.items() if v is not None}


async def _unresolved_link(  # type: ignore[no-untyped-def]
    session, user_id: int, *, status: OrderStatus = OrderStatus.UNKNOWN,
    with_trade: bool = True, fill_confirmed: bool = False,
    exchange_id: str | None = None,
) -> tuple[ExecutionOrder, Trade | None]:
    """Вход LINK старше окна и (по умолчанию) предварительная сделка по плану:
    14.398 × 2037.8, без комиссии — так её пишет record_entry_trade до факта."""
    n = await _notification(session, user_id, "LINK-USDT")
    placed = datetime.now(UTC) - timedelta(minutes=11)
    trade = None
    if with_trade:
        trade = await TradeJournal(TradeRepository(session)).open_trade(
            user_id=user_id, symbol="LINK-USDT", side=TradeSide.LONG, entry_price=D("14.398"),
            quantity=D("2037.8"), leverage=10, stop_loss=D("13.526"), take_profit=D("16.263"),
            opened_at=placed, source=TradeSource.SIGNAL_EXECUTION, notification_id=n.id,
            account_mode=ExchangeKeyMode.DEMO,
            fill_confirmed=fill_confirmed,
        )
    entry = _row(user_id, n.id, "LINK-USDT", OrderRole.ENTRY, status, exchange_id,
                 trade_id=trade.id if trade else None, created_at=placed)
    session.add(entry)
    await session.commit()
    return entry, trade


def _entry_events(
    events: list[ReconciliationEvent], entry_id: int
) -> dict[str, ReconciliationEvent]:
    return {e.dedup_key: e for e in events if e.dedup_key.startswith(f"entry:{entry_id}")}


async def test_confirm_entry_brings_provisional_trade_to_live_fact(ctx) -> None:  # type: ignore[no-untyped-def]
    """B1: сделка OPEN, не подтверждена, ENTRY-исполнение есть, время и
    positionID в ответе — всё приводится к живому факту, одно уведомление;
    второй цикл вход не ищет и ничего не шлёт."""
    settings, db, session, user, demo = ctx
    entry, trade = await _unresolved_link(session, user.id)
    assert trade is not None
    demo.orders_by_cid[entry.client_order_id] = _live_entry()
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    row = (await _rows(db, "LINK-USDT"))[OrderRole.ENTRY]
    assert (row.status, row.exchange_order_id) == (OrderStatus.FILLED, LINK_ENTRY)
    t = await _trade(db, trade.id)
    assert t.fill_confirmed is True
    assert (t.entry_price, t.quantity, t.fees) == (D("14.4"), D("2037.8"), D("14.672461"))
    assert t.opened_at == LINK_FILLED_AT
    assert t.external_position_id == LINK_POSITION
    [fill] = t.fills
    assert (fill.price, fill.quantity, fill.fee) == (D("14.4"), D("2037.8"), D("14.672461"))
    assert (fill.external_fill_id, fill.executed_at) == (LINK_ENTRY, LINK_FILLED_AT)
    events = _entry_events(await _events(db, user.id), entry.id)
    confirmed = events[f"entry:{entry.id}:confirmed"]
    assert confirmed.kind is ReconciliationKind.ENTRY_CONFIRMED
    assert confirmed.resolved_at is not None
    [text] = [t for t in bot.sent if "найден на бирже" in t]
    assert "✅ Вход LINK-USDT LONG найден на бирже: 2037.8 по 14.4" in text
    assert f"Сделка #{trade.id} подтверждена фактом биржи" in text

    calls_before = demo.calls.count(ORDER_GET)
    await _run_reconciler(settings, db, bot)
    assert demo.calls.count(ORDER_GET) == calls_before
    assert sum("найден на бирже" in t for t in bot.sent) == 1


async def test_confirm_entry_without_fill_time_keeps_opened_at(ctx) -> None:  # type: ignore[no-untyped-def]
    """B2: в ответе нет ни updateTime, ни time (СИНТЕТИКА ИЗ ЖИВОГО #37,
    убраны оба) — цена, объём и комиссия подтверждаются, время — прежнее."""
    settings, db, session, user, demo = ctx
    entry, trade = await _unresolved_link(session, user.id)
    assert trade is not None
    opened_before = trade.opened_at
    demo.orders_by_cid[entry.client_order_id] = _live_entry(updateTime=None, time=None)

    await _run_reconciler(settings, db, FakeBot())

    t = await _trade(db, trade.id)
    assert t.fill_confirmed is True and t.entry_price == D("14.4")
    assert t.opened_at == opened_before
    [fill] = t.fills
    assert fill.executed_at == opened_before


async def test_confirm_entry_zero_position_id_is_not_recorded(ctx) -> None:  # type: ignore[no-untyped-def]
    """B3: positionID 0 (так он приходит живьём в GET условника, #38) —
    СИНТЕТИКА ИЗ ЖИВОГО #37, заменён positionID: позиция сделки не
    записывается."""
    settings, db, session, user, demo = ctx
    entry, trade = await _unresolved_link(session, user.id)
    assert trade is not None
    demo.orders_by_cid[entry.client_order_id] = _live_entry(positionID=0)

    await _run_reconciler(settings, db, FakeBot())

    t = await _trade(db, trade.id)
    assert t.fill_confirmed is True
    assert t.external_position_id is None


async def test_confirm_entry_trade_without_entry_fill_is_ambiguous(ctx) -> None:  # type: ignore[no-untyped-def]
    """B4 (решение 29.09): сделка OPEN без ENTRY-исполнения (record_entry_trade
    так не пишет, но строка могла потерять fill) — не пересчитываем (пустые
    fills обнулили бы цену и объём) и не подтверждаем: AMBIGUOUS
    entry:{id}:no_entry_fill, ENTRY_CONFIRMED нет, сделка как была."""
    settings, db, session, user, demo = ctx
    entry, _ = await _unresolved_link(session, user.id, with_trade=False)
    orphan_trade = Trade(
        user_id=user.id, symbol="LINK-USDT", side=TradeSide.LONG, entry_price=D("14.398"),
        quantity=D("2037.8"), leverage=10, opened_at=entry.created_at,
        source=TradeSource.SIGNAL_EXECUTION, status=TradeStatus.OPEN, fill_confirmed=False,
        account_mode=ExchangeKeyMode.DEMO,
    )
    session.add(orphan_trade)
    await session.flush()
    entry.trade_id = orphan_trade.id
    await session.commit()
    demo.orders_by_cid[entry.client_order_id] = _live_entry()
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)
    await _run_reconciler(settings, db, bot)

    t = await _trade(db, orphan_trade.id)
    assert (t.entry_price, t.quantity, t.fill_confirmed) == (D("14.398"), D("2037.8"), False)
    assert t.external_position_id is None and t.fills == []
    row = (await _rows(db, "LINK-USDT"))[OrderRole.ENTRY]
    assert (row.status, row.exchange_order_id) == (OrderStatus.FILLED, LINK_ENTRY)
    events = _entry_events(await _events(db, user.id), entry.id)
    assert set(events) == {f"entry:{entry.id}:no_entry_fill"}
    event = events[f"entry:{entry.id}:no_entry_fill"]
    assert event.kind is ReconciliationKind.AMBIGUOUS and event.resolved_at is None
    assert event.trade_id == orphan_trade.id
    detail = (
        f"Сделка #{orphan_trade.id} без исполнения входа в журнале — подтверждение не "
        "выполнено, проверь вручную"
    )
    assert event.detail == detail
    assert sum(detail in text for text in bot.sent) == 1
    assert not any("найден на бирже" in text for text in bot.sent)


async def test_confirm_entry_without_trade_is_permanent_ambiguous(ctx) -> None:  # type: ignore[no-untyped-def]
    """B5: вход исполнен, сделки в журнале нет — AMBIGUOUS no_trade, сделку
    не создаём, ENTRY_CONFIRMED нет. Вход FILLED и больше не ищется —
    расхождение остаётся открытым (задумано, 29.09)."""
    settings, db, session, user, demo = ctx
    entry, _ = await _unresolved_link(session, user.id, with_trade=False)
    demo.orders_by_cid[entry.client_order_id] = _live_entry()
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)
    await _run_reconciler(settings, db, bot)

    row = (await _rows(db, "LINK-USDT"))[OrderRole.ENTRY]
    assert (row.status, row.exchange_order_id, row.trade_id) == (
        OrderStatus.FILLED, LINK_ENTRY, None
    )
    events = _entry_events(await _events(db, user.id), entry.id)
    assert set(events) == {f"entry:{entry.id}:no_trade"}
    no_trade = events[f"entry:{entry.id}:no_trade"]
    assert no_trade.kind is ReconciliationKind.AMBIGUOUS and no_trade.resolved_at is None
    assert demo.calls.count(ORDER_GET) == 1
    assert sum(f"вход {entry.client_order_id} исполнен на бирже" in t for t in bot.sent) == 1
    # Живая позиция LINK без сделки в журнале — ещё и ORPHAN, тоже один раз.
    assert sum("позиция LONG 2037.8 по 14.400 на бирже" in t for t in bot.sent) == 1
    async with db.session() as s:
        count = await s.scalar(
            select(func.count()).select_from(Trade).where(
                Trade.user_id == user.id, Trade.symbol == "LINK-USDT"
            )
        )
    assert count == 0


@pytest.mark.parametrize("status", [TradeStatus.CANCELLED, TradeStatus.CLOSED])
async def test_confirm_entry_for_closed_or_cancelled_trade_is_fact_only(  # type: ignore[no-untyped-def]
    ctx, status
) -> None:
    """B6: сделку уже отменили или закрыли руками — журнал не трогаем, только
    событие-факт (задумано, 29.09)."""
    settings, db, session, user, demo = ctx
    entry, trade = await _unresolved_link(session, user.id)
    assert trade is not None
    trade.status = status
    if status is TradeStatus.CLOSED:
        trade.closed_at = datetime.now(UTC)
    await session.commit()
    demo.orders_by_cid[entry.client_order_id] = _live_entry()
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    t = await _trade(db, trade.id)
    assert (t.status, t.fill_confirmed, t.entry_price) == (status, False, D("14.398"))
    assert t.external_position_id is None
    row = (await _rows(db, "LINK-USDT"))[OrderRole.ENTRY]
    assert row.status is OrderStatus.FILLED
    events = _entry_events(await _events(db, user.id), entry.id)
    assert events[f"entry:{entry.id}:confirmed"].kind is ReconciliationKind.ENTRY_CONFIRMED
    [text] = [t for t in bot.sent if "найден на бирже" in t]
    assert "подтверждена" not in text


async def test_confirm_entry_already_confirmed_trade_is_fact_only(ctx) -> None:  # type: ignore[no-untyped-def]
    """B7: вход UNKNOWN, а сделка уже подтверждена (ранний выход на 552 —
    только для SUBMITTED) — журнал не трогаем, только факт."""
    settings, db, session, user, demo = ctx
    entry, trade = await _unresolved_link(session, user.id, fill_confirmed=True)
    assert trade is not None
    demo.orders_by_cid[entry.client_order_id] = _live_entry()
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    t = await _trade(db, trade.id)
    assert (t.entry_price, t.fees) == (D("14.398"), D("0"))
    assert (await _rows(db, "LINK-USDT"))[OrderRole.ENTRY].status is OrderStatus.FILLED
    [text] = [t for t in bot.sent if "найден на бирже" in t]
    assert "подтверждена" not in text


async def test_confirm_entry_keeps_existing_exchange_order_id(ctx) -> None:  # type: ignore[no-untyped-def]
    """B8: у строки входа уже есть exchange_order_id (из ответа POST) — не
    перезаписывается ответом поиска. Значение в строке — синтетика, отличное
    от живого, чтобы перезапись была видна."""
    settings, db, session, user, demo = ctx
    entry, trade = await _unresolved_link(
        session, user.id, status=OrderStatus.SUBMITTED, exchange_id="2104122757776150000"
    )
    assert trade is not None
    demo.orders_by_cid[entry.client_order_id] = _live_entry()

    await _run_reconciler(settings, db, FakeBot())

    row = (await _rows(db, "LINK-USDT"))[OrderRole.ENTRY]
    assert (row.status, row.exchange_order_id) == (OrderStatus.FILLED, "2104122757776150000")
    t = await _trade(db, trade.id)
    assert t.fill_confirmed is True
    assert [f.external_fill_id for f in t.fills] == [LINK_ENTRY]


async def test_submitted_entry_with_confirmed_trade_is_not_searched(ctx) -> None:  # type: ignore[no-untyped-def]
    """Строка 552: SUBMITTED, а сделка уже подтверждена — исполнение в
    журнале, поиска нет, ничего не пишется."""
    settings, db, session, user, demo = ctx
    entry, _ = await _unresolved_link(
        session, user.id, status=OrderStatus.SUBMITTED, fill_confirmed=True
    )
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    assert ORDER_GET not in demo.calls
    assert (await _rows(db, "LINK-USDT"))[OrderRole.ENTRY].status is OrderStatus.SUBMITTED
    assert _entry_events(await _events(db, user.id), entry.id) == {}


async def test_entry_lookup_incomplete_retries_next_cycle_silently(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """Строки 570-574: ответ поиска не разобран (FILLED с пустой commission —
    СИНТЕТИКА ИЗ ЖИВОГО #37) — WARNING, ничего не пишется, на следующем
    цикле поиск повторяется."""
    settings, db, session, user, demo = ctx
    entry, trade = await _unresolved_link(session, user.id)
    assert trade is not None
    demo.orders_by_cid[entry.client_order_id] = _live_entry(commission="")
    bot = FakeBot()

    with caplog.at_level("WARNING", logger="app.workers.reconciler"):
        await _run_reconciler(settings, db, bot)
        await _run_reconciler(settings, db, bot)

    assert demo.calls.count(ORDER_GET) == 2
    assert "Поиск входа по client_order_id не удался" in caplog.text
    assert (await _rows(db, "LINK-USDT"))[OrderRole.ENTRY].status is OrderStatus.UNKNOWN
    assert (await _trade(db, trade.id)).fill_confirmed is False
    assert _entry_events(await _events(db, user.id), entry.id) == {}
    assert not any("найден на бирже" in t for t in bot.sent)


async def test_entry_not_found_but_position_exists_is_ambiguous(ctx) -> None:  # type: ignore[no-untyped-def]
    """Строки 580-581: биржа ответила 109421 (живой код), а живая позиция LINK
    есть — вывода нет, AMBIGUOUS, вход не NOT_PLACED."""
    settings, db, session, user, demo = ctx
    entry, trade = await _unresolved_link(session, user.id)
    assert trade is not None
    demo.unknown_cids.add(entry.client_order_id)

    await _run_reconciler(settings, db, FakeBot())

    assert (await _rows(db, "LINK-USDT"))[OrderRole.ENTRY].status is OrderStatus.UNKNOWN
    event = _entry_events(await _events(db, user.id), entry.id)[f"entry:{entry.id}"]
    assert event.kind is ReconciliationKind.AMBIGUOUS and event.resolved_at is None
    assert "не найден, а позиция LONG по символу есть" in event.detail
    assert (await _trade(db, trade.id)).status is TradeStatus.OPEN


async def test_not_placed_with_cancelled_trade_does_not_cancel_again(ctx) -> None:  # type: ignore[no-untyped-def]
    """Ветка 590→593: вход не выставлен, позиции нет, а предварительная сделка
    уже отменена — NOT_PLACED, сделку не трогаем, в тексте нет строки об
    отмене."""
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    n = await _notification(session, user.id, "ADA-USDT")
    trade = await TradeJournal(TradeRepository(session)).open_trade(
        user_id=user.id, symbol="ADA-USDT", side=TradeSide.LONG, entry_price=D("1"),
        quantity=D("10"), source=TradeSource.SIGNAL_EXECUTION, notification_id=n.id,
        account_mode=ExchangeKeyMode.DEMO,
        fill_confirmed=False,
    )
    trade.status = TradeStatus.CANCELLED
    entry = _row(user.id, n.id, "ADA-USDT", OrderRole.ENTRY, OrderStatus.UNKNOWN, None,
                 trade_id=trade.id, created_at=datetime.now(UTC) - timedelta(minutes=11))
    session.add(entry)
    await session.commit()
    demo.unknown_cids.add(entry.client_order_id)
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    assert (await _rows(db, "ADA-USDT"))[OrderRole.ENTRY].status is OrderStatus.NOT_PLACED
    assert (await _trade(db, trade.id)).status is TradeStatus.CANCELLED
    [text] = [t for t in bot.sent if "не выставлен на бирже" in t]
    assert "Предварительная сделка" not in text


# --- 29.09: история ордеров не разбирается дольше 30 минут -------------------


class _Clock:
    """Время воркера сверки: datetime.now модуля app.workers.reconciler."""

    def __init__(self, now: datetime) -> None:
        self.now = now


def _freeze(monkeypatch, clock: _Clock) -> None:  # type: ignore[no-untyped-def]
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return clock.now if tz is None else clock.now.astimezone(tz)

    monkeypatch.setattr("app.workers.reconciler.datetime", Frozen)


def _unparsed_sol_history(demo: LiveDemo) -> None:
    """СИНТЕТИКА ИЗ ЖИВОГО allOrders SOL: у исполненного стопа-выхода
    commission "" — строгий разбор отказывает."""
    orders = live_items("allOrders SOL")
    [child] = [o for o in orders if str(o["orderId"]) == SOL_CHILD]
    child["commission"] = ""
    demo.all_orders["SOL-USDT"] = orders


def _reconciler(settings, db, bot):  # type: ignore[no-untyped-def]
    from app.workers.reconciler import Reconciler

    return Reconciler(bot, db, settings, SecretCipher(settings.encryption_key.get_secret_value()))


async def _open_events(db, user_id: int) -> list[ReconciliationEvent]:  # type: ignore[no-untyped-def]
    async with db.session() as s:
        rows = await s.scalars(
            select(ReconciliationEvent).where(ReconciliationEvent.user_id == user_id)
        )
        return list(rows)


async def test_history_unparsed_over_30_min_is_one_event(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """До 30 минут подряд — только WARNING и ошибка в пульсе каждый цикл;
    дольше — одно событие AMBIGUOUS владельцу (history:{id}:unparsed), повтора
    нет. Раньше сбой разбора обрывал сверку пользователя, события не было
    никогда."""
    settings, db, session, user, demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id)
    _unparsed_sol_history(demo)
    clock = _Clock(datetime.now(UTC))
    _freeze(monkeypatch, clock)
    bot = FakeBot()
    reconciler = _reconciler(settings, db, bot)
    start = clock.now

    for minutes in (0, 29):
        clock.now = start + timedelta(minutes=minutes)
        await reconciler.run()
    assert bot.sent == []

    for minutes in (31, 40):
        clock.now = start + timedelta(minutes=minutes)
        await reconciler.run()

    [text] = bot.sent
    assert text.startswith("⚠️ Сверка с биржей, SOL-USDT: история ордеров сделки")
    assert f"#{sol_id}" in text and "commission" in text
    [event] = [e for e in await _open_events(db, user.id) if e.dedup_key.startswith("history:")]
    assert (event.kind, event.dedup_key, event.resolved_at) == (
        ReconciliationKind.AMBIGUOUS, f"history:{sol_id}:unparsed", None
    )
    assert (await _trade(db, sol_id)).status is TradeStatus.OPEN
    assert reconciler.pulse.window(clock.now).errors == 4


async def test_history_parsed_again_resolves_event_and_closes_trade(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """История снова разбирается — событие разрешено, выход записан фактом."""
    settings, db, session, user, demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id)
    _unparsed_sol_history(demo)
    clock = _Clock(datetime.now(UTC))
    _freeze(monkeypatch, clock)
    bot = FakeBot()
    reconciler = _reconciler(settings, db, bot)
    start = clock.now
    for minutes in (0, 31):
        clock.now = start + timedelta(minutes=minutes)
        await reconciler.run()
    assert len(bot.sent) == 1

    demo.all_orders["SOL-USDT"] = live_items("allOrders SOL")
    clock.now = start + timedelta(minutes=32)
    await reconciler.run()

    [event] = [e for e in await _open_events(db, user.id) if e.dedup_key.startswith("history:")]
    assert event.resolved_at is not None
    assert (await _trade(db, sol_id)).status is TradeStatus.CLOSED
    assert "закрыта по стопу" in bot.sent[-1]


async def test_history_unparsed_does_not_stop_user_reconcile(ctx) -> None:  # type: ignore[no-untyped-def]
    """Сбой разбора истории одной сделки не обрывает сверку пользователя:
    позиция без сделки в журнале всё равно найдена. Раньше исключение
    выходило из цикла сделок, и дальше сверка не шла. СИНТЕТИКА ИЗ ЖИВОГО
    positions all: копия позиции LINK с символом BTC-USDT."""
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    _unparsed_sol_history(demo)
    [link] = live_items("positions all")
    demo.positions = [link, {**link, "symbol": "BTC-USDT", "positionId": "1"}]
    bot = FakeBot()

    await _reconciler(settings, db, bot).run()

    assert any("BTC-USDT" in text for text in bot.sent)


# --- 05.10.2026: сделка, открытая из бота (TradeSource.BOT) ------------------------


async def _bot_open_trade(  # type: ignore[no-untyped-def]
    session, user_id: int, symbol: str, *, entry: str, qty: str, entry_id: str,
    stop_id: str, take_id: str, opened_at: datetime, fee: str,
) -> Trade:
    """Как _bot_trade, но открытие из бота: без уведомления сигнала, стоп и
    тейк связаны со сделкой через trade_id (вложенные — без clientOrderId)."""
    trade = await TradeJournal(TradeRepository(session)).open_trade(
        user_id=user_id, symbol=symbol, side=TradeSide.LONG, entry_price=D(entry),
        quantity=D(qty), leverage=10, opened_at=opened_at, source=TradeSource.BOT,
        account_mode=ExchangeKeyMode.DEMO, external_fill_id=entry_id, fee=D(fee),
    )

    def row(role: OrderRole, order_type: OrderType, status: OrderStatus, oid: str,
            cid: str | None) -> ExecutionOrder:
        return ExecutionOrder(
            account_mode=ExchangeKeyMode.DEMO,
            user_id=user_id, trade_id=trade.id, client_order_id=cid, exchange_order_id=oid,
            symbol=symbol, side=OrderSide.BUY if role is OrderRole.ENTRY else OrderSide.SELL,
            position_side=TradeSide.LONG, order_type=order_type, role=role, status=status,
        )

    session.add_all([
        row(OrderRole.ENTRY, OrderType.MARKET, OrderStatus.FILLED, entry_id,
            f"to{trade.id}u{user_id}e"),
        row(OrderRole.STOP_LOSS, OrderType.STOP_MARKET, OrderStatus.SUBMITTED, stop_id, None),
        row(OrderRole.TAKE_PROFIT, OrderType.TAKE_PROFIT_MARKET, OrderStatus.SUBMITTED,
            take_id, None),
    ])
    await session.commit()
    return trade


async def test_bot_opened_trade_closed_by_our_stop(ctx) -> None:  # type: ignore[no-untyped-def]
    """Стоп сделки BOT узнаётся по trade_id (не по уведомлению сигнала):
    выход — «по стопу», строка стопа FILLED, тейк снят биржей."""
    settings, db, session, user, _demo = ctx
    sol = await _bot_open_trade(
        session, user.id, "SOL-USDT", entry="123.021", qty="1362.07", entry_id=SOL_ENTRY,
        stop_id=SOL_STOP, take_id=SOL_TAKE, fee="83.781520",
        opened_at=datetime(2026, 9, 27, 14, 15, 30, 300000, tzinfo=UTC),
    )
    await _bot_open_trade(
        session, user.id, "LINK-USDT", entry="14.4", qty="2037.8", entry_id=LINK_ENTRY,
        stop_id=LINK_STOP, take_id=LINK_TAKE, fee="14.672461",
        opened_at=datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC),
    )
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    closed = await _trade(db, sol.id)
    assert closed.status is TradeStatus.CLOSED
    assert closed.exit_reason == EXIT_STOP_LOSS
    assert {f.external_fill_id for f in closed.fills} == {SOL_ENTRY, SOL_CHILD}
    rows = await _rows(db, "SOL-USDT")
    assert rows[OrderRole.STOP_LOSS].status is OrderStatus.FILLED
    assert len(bot.sent) == 1 and "закрыта по стопу" in bot.sent[0]


async def test_bot_opened_trade_missing_stop_alarm(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    settings.reconciler_stop_check_every = 1
    await _bot_open_trade(
        session, user.id, "LINK-USDT", entry="14.4", qty="2037.8", entry_id=LINK_ENTRY,
        stop_id=LINK_STOP, take_id=LINK_TAKE, fee="14.672461",
        opened_at=datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC),
    )
    demo.link_open_orders = []
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    assert any(t.startswith("⚠️ ПОЗИЦИЯ БЕЗ СТОПА: LINK-USDT") for t in bot.sent)


# --- 09.10: уведомления сверки — в журнал исходящих (очередь A.2) ---------------


class MessageBot(FakeBot):
    """Отдаёт Message, как настоящий Telegram: журнал пишет его message_id."""

    def __init__(self, first_id: int = 5001) -> None:
        super().__init__()
        self.next_id = first_id

    async def send_message(self, chat_id: int, text: str, reply_markup=None):  # type: ignore[no-untyped-def]
        from aiogram.types import Chat, Message

        self.sent.append(text)
        self.next_id += 1
        return Message(
            message_id=self.next_id, date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"), text=text,
        )


async def _outgoing(db, user_id: int) -> list[Any]:  # type: ignore[no-untyped-def]
    from app.database.models.outgoing_message import OutgoingMessage

    async with db.session() as s:
        stmt = select(OutgoingMessage).where(OutgoingMessage.user_id == user_id)
        return list(await s.scalars(stmt.order_by(OutgoingMessage.id)))


async def test_close_notification_is_recorded_after_commit(ctx) -> None:  # type: ignore[no-untyped-def]
    """SOL #4 закрыта стопом: уведомление — строка outgoing_messages с
    message_id, видом события и сделкой. Запись идёт после commit: строка
    сделки под FOR UPDATE, а журнал пишет своей сессией с FK на неё — внутри
    транзакции сверка ждала бы сама себя (wait_for ловит зависание)."""
    import asyncio

    settings, db, session, user, _demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id)
    bot = MessageBot()

    await asyncio.wait_for(_run_reconciler(settings, db, bot), timeout=30)

    [text] = bot.sent
    [row] = await _outgoing(db, user.id)
    assert (row.kind, row.trade_id, row.message_id, row.chat_id, row.text) == (
        "RECON_CLOSED_STOP_LOSS", sol_id, bot.next_id, user.telegram_id, text,
    )


async def test_redelivered_notification_is_recorded(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.workers.reconciler as reconciler_module

    settings, db, session, user, _demo = ctx
    await _pending_event(session, user.id, age=timedelta(minutes=1))
    monkeypatch.setattr(reconciler_module, "ExchangeFactory", _DownFactory)

    bot = MessageBot()
    await _run_reconciler(settings, db, bot)

    [row] = await _outgoing(db, user.id)
    assert (row.kind, row.trade_id, row.text) == (
        "RECON_CLOSED_STOP_LOSS", None, "🛑 SOL-USDT LONG закрыта по стопу на бирже",
    )


async def test_failed_notification_is_not_recorded(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, _demo = ctx
    await _seed_live(session, user.id)

    await _run_reconciler(settings, db, NetworkFailBot())

    assert await _outgoing(db, user.id) == []


def test_recon_kinds_fit_outgoing_kind_column() -> None:
    from app.database.models.outgoing_message import OutgoingMessage

    limit = OutgoingMessage.__table__.c.kind.type.length
    assert all(len(f"RECON_{k.value}") <= limit for k in ReconciliationKind)


# --- 10.10: A.3 — строки стопа/тейка закрытых сделок по факту биржи ---------------

XRP = "XRP-USDT"


def _xrp_hist(oid: str, otype: str, status: str, trigger: str = "0", *,
              created: datetime | None = None, updated: datetime | None = None) -> dict[str, Any]:
    item = dict(live_items("allOrders SOL")[1])
    item.update(symbol=XRP, orderId=int(oid), type=otype, status=status,
                triggerOrderId=int(trigger), stopPrice="1.3000", origQty="100")
    if created is not None:
        item["time"] = int(created.timestamp() * 1000)
    if updated is not None:
        item["updateTime"] = int(updated.timestamp() * 1000)
    return item


def _xrp_open(oid: str) -> dict[str, Any]:
    item = dict(live_items("openOrders LINK")[0])
    item.update(symbol=XRP, orderId=int(oid), stopPrice="1.3000", origQty="100")
    return item


async def _closed_xrp_trade(  # type: ignore[no-untyped-def]
    session, user_id: int, rows: list[tuple[OrderRole, OrderStatus, str]], *,
    mode: ExchangeKeyMode = ExchangeKeyMode.DEMO, age: timedelta = timedelta(hours=2),
    tag: str = "1",
) -> tuple[Trade, list[ExecutionOrder]]:
    """Сделка BOT, уже закрытая («Закрыть всё» / аварийно), со строками
    стопа/тейка, которые к моменту закрытия не закрылись (гонка)."""
    journal = TradeJournal(TradeRepository(session))
    opened = datetime.now(UTC) - age
    trade = await journal.open_trade(
        user_id=user_id, symbol=XRP, side=TradeSide.LONG, entry_price=D("1.4"),
        quantity=D(100), leverage=10, opened_at=opened, source=TradeSource.BOT,
        account_mode=mode, external_fill_id=f"9{user_id}{tag}1", fee=D("0.07"),
    )
    await journal.close_trade(
        trade, exit_price=D("1.39"), fee=D("0.07"), exit_reason="Закрыть всё",
        closed_at=opened + timedelta(minutes=5), external_fill_id=f"9{user_id}{tag}2",
    )
    made = [
        ExecutionOrder(
            account_mode=ExchangeKeyMode.DEMO,
            user_id=user_id, trade_id=trade.id, exchange_order_id=oid, symbol=XRP,
            side=OrderSide.SELL, position_side=TradeSide.LONG,
            order_type=(OrderType.STOP_MARKET if role is OrderRole.STOP_LOSS
                        else OrderType.TAKE_PROFIT_MARKET),
            role=role, status=status,
            error_code="NOT_FOUND" if status is OrderStatus.REJECTED else None,
        )
        for role, status, oid in rows
    ]
    session.add_all(made)
    await session.commit()
    return trade, made


async def _orders(db, ids: list[int]) -> dict[int, ExecutionOrder]:  # type: ignore[no-untyped-def]
    async with db.session() as s:
        rows = await s.scalars(select(ExecutionOrder).where(ExecutionOrder.id.in_(ids)))
        return {r.id: r for r in rows}


def _sweep_calls(demo: LiveDemo) -> list[str]:
    return [c for c in demo.calls if c.endswith(("/openOrders", "/allOrders"))]


async def test_sweep_settles_closed_trade_rows_by_history(ctx) -> None:  # type: ignore[no-untyped-def]
    """Риск 3: сделка закрыта, строки остались SUBMITTED (биржа не успела
    снять к моменту чтения). Следующий цикл: в истории CANCELLED в допуске
    от нашего закрытия → EXCHANGE; снят задолго до закрытия (руками) → NULL;
    нет ни в истории, ни в openOrders → CANCELLED без источника; REJECTED
    (старая строка Т4) обход не трогает."""
    settings, db, session, user, demo = ctx
    demo.positions = []
    trade, rows = await _closed_xrp_trade(session, user.id, [
        (OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, "7001"),
        (OrderRole.TAKE_PROFIT, OrderStatus.UNKNOWN, "7002"),
        (OrderRole.STOP_LOSS, OrderStatus.REJECTED, "7003"),
        (OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, "7004"),
    ])
    closed = trade.closed_at
    assert closed is not None
    demo.all_orders[XRP] = [
        _xrp_hist(f"9{user.id}12", "MARKET", "FILLED", updated=closed),
        _xrp_hist("7001", "STOP_MARKET", "CANCELLED", updated=closed + timedelta(seconds=1)),
        _xrp_hist("7004", "STOP_MARKET", "CANCELLED", updated=closed - timedelta(seconds=139)),
    ]

    await _run_reconciler(settings, db, FakeBot())

    got = await _orders(db, [r.id for r in rows])
    stop, take, rejected, bridge = (got[r.id] for r in rows)
    assert (stop.status, stop.cancel_source) == (OrderStatus.CANCELLED, CancelSource.EXCHANGE)
    assert (take.status, take.cancel_source) == (OrderStatus.CANCELLED, None)
    assert rejected.status is OrderStatus.REJECTED and rejected.error_code == "NOT_FOUND"
    assert (bridge.status, bridge.cancel_source) == (OrderStatus.CANCELLED, None)
    assert _sweep_calls(demo).count("/openApi/swap/v2/trade/allOrders") == 1


async def test_sweep_without_our_closing_order_in_history_is_null(ctx) -> None:  # type: ignore[no-untyped-def]
    """Уточнение владельца 23.10 (1): T — updateTime НАШЕГО закрытия по
    orderId из журнала. Его нет в выдаче — NULL, даже если рядом чужой
    MARKET FILLED в ту же секунду."""
    settings, db, session, user, demo = ctx
    demo.positions = []
    trade, [stop] = await _closed_xrp_trade(
        session, user.id, [(OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, "7011")]
    )
    closed = trade.closed_at
    assert closed is not None
    demo.all_orders[XRP] = [
        _xrp_hist("7099", "MARKET", "FILLED", updated=closed),
        _xrp_hist("7011", "STOP_MARKET", "CANCELLED", updated=closed + timedelta(seconds=1)),
    ]

    await _run_reconciler(settings, db, FakeBot())

    got = (await _orders(db, [stop.id]))[stop.id]
    assert (got.status, got.cancel_source) == (OrderStatus.CANCELLED, None)


async def test_sweep_window_starts_at_row_creation_not_close(ctx) -> None:  # type: ignore[no-untyped-def]
    """Уточнение владельца 23.10 (2): allOrders отбирает по созданию ордера.
    Стоп создан задолго до закрытия — окно от создания строки (минус запас)
    его захватывает: в выдаче → EXCHANGE. Окно от закрытия его бы потеряло."""
    settings, db, session, user, demo = ctx
    demo.positions = []
    demo.window_by_created = True
    trade, [stop] = await _closed_xrp_trade(
        session, user.id, [(OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, "7021")],
        age=timedelta(days=2),
    )
    closed = trade.closed_at
    assert closed is not None
    created = trade.opened_at + timedelta(seconds=30)
    stop.created_at = created
    trade.closed_at = closed = created + timedelta(days=1)
    await session.commit()
    demo.all_orders[XRP] = [
        _xrp_hist(f"9{user.id}12", "MARKET", "FILLED", created=closed, updated=closed),
        _xrp_hist("7021", "STOP_MARKET", "CANCELLED", created=created,
                  updated=closed + timedelta(seconds=1)),
    ]

    await _run_reconciler(settings, db, FakeBot())

    got = (await _orders(db, [stop.id]))[stop.id]
    assert (got.status, got.cancel_source) == (OrderStatus.CANCELLED, CancelSource.EXCHANGE)
    [(_symbol, start, _end)] = demo.all_orders_windows
    assert start <= int(created.timestamp() * 1000) - 60_000


async def test_sweep_full_page_is_incomplete(ctx) -> None:  # type: ignore[no-untyped-def]
    """Уточнение владельца 23.10 (2): выдача упёрлась в лимит 500 — окно
    неполное, «нет в истории» строку не закрывает."""
    from app.exchanges.base import ALL_ORDERS_LIMIT

    settings, db, session, user, demo = ctx
    demo.positions = []
    _, [stop] = await _closed_xrp_trade(
        session, user.id, [(OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, "7031")]
    )
    demo.all_orders[XRP] = [
        _xrp_hist(str(8_000_000 + i), "STOP_MARKET", "CANCELLED") for i in range(ALL_ORDERS_LIMIT)
    ]

    await _run_reconciler(settings, db, FakeBot())

    got = (await _orders(db, [stop.id]))[stop.id]
    assert got.status is OrderStatus.SUBMITTED and got.cancel_source is None


async def test_sweep_fired_stop_by_child_is_filled(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, demo = ctx
    demo.positions = []
    _, [stop] = await _closed_xrp_trade(
        session, user.id, [(OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, "7101")]
    )
    demo.all_orders[XRP] = [_xrp_hist("7199", "STOP_MARKET", "FILLED", trigger="7101")]

    await _run_reconciler(settings, db, FakeBot())

    got = (await _orders(db, [stop.id]))[stop.id]
    assert got.status is OrderStatus.FILLED and got.cancel_source is None


async def test_sweep_standing_order_untouched_warning_once(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """Ордер закрытой сделки ещё в openOrders — строку не трогать; WARNING —
    один раз на строку, хотя обход идёт каждый цикл."""
    from app.workers.reconciler import Reconciler

    settings, db, session, user, demo = ctx
    demo.positions = []
    _, [stop] = await _closed_xrp_trade(
        session, user.id, [(OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, "7201")]
    )
    demo.all_orders[XRP] = []
    demo.open_orders[XRP] = [_xrp_open("7201")]
    reconciler = Reconciler(
        FakeBot(), db, settings, SecretCipher(settings.encryption_key.get_secret_value()), None
    )

    with caplog.at_level("WARNING"):
        await reconciler.run()
        await reconciler.run()

    got = (await _orders(db, [stop.id]))[stop.id]
    assert got.status is OrderStatus.SUBMITTED and got.cancel_source is None
    warned = [r for r in caplog.records if "условный ордер на бирже стоит" in r.getMessage()]
    assert len(warned) == 1


async def test_sweep_skips_other_account_and_old_trades_without_requests(ctx) -> None:  # type: ignore[no-untyped-def]
    """Строки LIVE по openOrders DEMO закрылись бы ложно; сделка старше окна
    истории (7 дней) — «ордера нет» не доказать. Ни строк, ни запросов."""
    settings, db, session, user, demo = ctx
    demo.positions = []
    _, [live] = await _closed_xrp_trade(
        session, user.id, [(OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, "7301")],
        mode=ExchangeKeyMode.LIVE, tag="1",
    )
    _, [old] = await _closed_xrp_trade(
        session, user.id, [(OrderRole.STOP_LOSS, OrderStatus.SUBMITTED, "7302")],
        age=timedelta(days=8), tag="2",
    )

    await _run_reconciler(settings, db, FakeBot())

    got = await _orders(db, [live.id, old.id])
    assert {r.status for r in got.values()} == {OrderStatus.SUBMITTED}
    assert _sweep_calls(demo) == []


async def test_closes_fully_settles_every_row_not_only_last(ctx) -> None:  # type: ignore[no-untyped-def]
    """Сверка закрыла сделку стопом: сработавший (последний стоп) — FILLED,
    более ранний стоп той же сделки (вложенный, не снятый в базе) и тейк —
    по истории, а не только «последние по id»."""
    settings, db, session, user, demo = ctx
    settings.reconciler_stop_check_every = 1
    early = "2104213344721920077"
    demo.all_orders["SOL-USDT"] = [
        *live_items("allOrders SOL"), _xrp_hist(early, "STOP_MARKET", "CANCELLED"),
    ]
    demo.all_orders["SOL-USDT"][-1]["symbol"] = "SOL-USDT"
    sol = await _bot_open_trade(
        session, user.id, "SOL-USDT", entry="123.021", qty="1362.07", entry_id=SOL_ENTRY,
        stop_id=SOL_STOP, take_id=SOL_TAKE, fee="83.781520",
        opened_at=datetime(2026, 9, 27, 14, 15, 30, 300000, tzinfo=UTC),
    )
    extra = ExecutionOrder(
        account_mode=ExchangeKeyMode.DEMO,
        user_id=user.id, trade_id=sol.id, exchange_order_id=early, symbol="SOL-USDT",
        side=OrderSide.SELL, position_side=TradeSide.LONG, order_type=OrderType.STOP_MARKET,
        role=OrderRole.STOP_LOSS, status=OrderStatus.SUBMITTED,
    )
    session.add(extra)
    await session.commit()
    extra_id = extra.id
    demo.positions = [p for p in demo.positions if p["symbol"] != "SOL-USDT"]

    await _run_reconciler(settings, db, FakeBot())

    async with db.session() as s:
        rows = list(await s.scalars(
            select(ExecutionOrder).where(
                ExecutionOrder.trade_id == sol.id, ExecutionOrder.role != OrderRole.ENTRY
            )
        ))
    by_oid = {r.exchange_order_id: r for r in rows}
    assert by_oid[SOL_STOP].status is OrderStatus.FILLED
    assert by_oid[SOL_TAKE].status is OrderStatus.CANCELLED
    assert by_oid[early].id == extra_id
    assert (by_oid[early].status, by_oid[early].cancel_source) == (
        OrderStatus.CANCELLED, CancelSource.EXCHANGE,
    )


async def test_closes_fully_standing_rows_untouched(ctx) -> None:  # type: ignore[no-untyped-def]
    """LINK закрыта ручным стопом, а openOrders ещё отдаёт её стоп и тейк —
    строки не трогаем (ордер стоит — не CANCELLED)."""
    settings, db, session, user, demo = ctx
    link = await _seed_link_manual_stop(session, demo, user.id)
    demo.link_open_orders = live_items("openOrders LINK")

    await _run_reconciler(settings, db, FakeBot())

    assert (await _trade(db, link.id)).status is TradeStatus.CLOSED
    rows = await _rows(db, "LINK-USDT")
    assert rows[OrderRole.STOP_LOSS].status is OrderStatus.SUBMITTED
    assert rows[OrderRole.TAKE_PROFIT].status is OrderStatus.SUBMITTED


# --- деплой 4, 4.4: добор строки закрытия по cid ------------------------------------------


async def test_close_row_unknown_backfilled_and_action_confirmed(ctx) -> None:  # type: ignore[no-untyped-def]
    """#37 (11.10): «Закрыть всё» исполнилось, строка CLOSE — UNKNOWN, действие
    FAILED «не подтверждено». Проход сверки (с B.9 — восстановление действий в
    нём) добирает строку по cid (FILLED с ценой) и переводит действие в DONE с
    пометкой."""
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    action = PositionAction(
        account_mode=ExchangeKeyMode.DEMO,
        user_id=user.id, symbol="LINK-USDT", side=TradeSide.LONG,
        kind=PositionActionKind.CLOSE_FULL, status=PositionActionStatus.FAILED, params={},
        error_code="POSITION_CHANGED", error_message="Исполнение закрытия не подтверждено.",
    )
    session.add(action)
    await session.flush()
    cid = f"tm{action.id}u{user.id}C"
    row = ExecutionOrder(
        account_mode=ExchangeKeyMode.DEMO,
        user_id=user.id, position_action_id=action.id, client_order_id=cid,
        symbol="LINK-USDT", side=OrderSide.SELL, position_side=TradeSide.LONG,
        order_type=OrderType.MARKET, role=OrderRole.CLOSE, status=OrderStatus.UNKNOWN,
        quantity=D("2037.8"),
    )
    session.add(row)
    await session.commit()
    demo.orders_by_cid[cid] = _live_entry(side="SELL", clientOrderId=cid)

    # B.9: строки действий добирает восстановление (под локом — нужен Redis).
    await _run_reconciler(settings, db, FakeBot(), FakeRedis())

    async with db.session() as s:
        fresh = await s.get(ExecutionOrder, row.id)
        done = await s.get(PositionAction, action.id)
    assert fresh is not None and fresh.status is OrderStatus.FILLED and fresh.price is not None
    assert done is not None and done.status is PositionActionStatus.DONE
    assert done.error_message is None and done.params["confirmed_by"] == "добор по cid"


# --- деплой 4, 4.5: выход кнопки «Закрыть» узнан по строке CLOSE ---------------------------


async def test_close_button_exit_is_closed_by_bot(ctx) -> None:  # type: ignore[no-untyped-def]
    """#29 (11.10): позицию закрыл маркет «Закрыть всё», выход не записался —
    сверка пишет его как «через бота» (строка CLOSE сделки с этим orderId), а не
    «вне бота». Живой ордер SOL — как маркет без triggerOrderId."""
    settings, db, session, user, demo = ctx
    sol_id, _link_id = await _seed_live(session, user.id)
    [child] = [o for o in demo.all_orders["SOL-USDT"] if str(o["orderId"]) == SOL_CHILD]
    child.update(type="MARKET", triggerOrderId="", stopPrice="")
    session.add(ExecutionOrder(
        account_mode=ExchangeKeyMode.DEMO,
        user_id=user.id, trade_id=sol_id, client_order_id=f"tm99u{user.id}C",
        exchange_order_id=SOL_CHILD, symbol="SOL-USDT", side=OrderSide.SELL,
        position_side=TradeSide.LONG, order_type=OrderType.MARKET, role=OrderRole.CLOSE,
        status=OrderStatus.FILLED, quantity=D("1362.07"), price=D("121.611"),
    ))
    await session.commit()
    bot = FakeBot()

    await _run_reconciler(settings, db, bot)

    sol = await _trade(db, sol_id)
    assert sol.status is TradeStatus.CLOSED and sol.exit_reason == "Закрыто через бота"
    kinds = [e.kind for e in await _events(db, user.id) if e.trade_id == sol_id]
    assert ReconciliationKind.CLOSED_BY_BOT in kinds
    assert ReconciliationKind.CLOSED_OUTSIDE_BOT not in kinds
    assert len(bot.sent) == 1 and "SOL-USDT LONG закрыта кнопкой в боте" in bot.sent[0]


async def test_fired_stop_filled_even_without_open_orders(ctx) -> None:  # type: ignore[no-untyped-def]
    """4.5: сработал closePosition-стоп, у сделки есть и ранний вложенный стоп
    (id новее). openOrders не получены — остальные строки ждут цикла, но
    сработавший — FILLED сразу, и это он, а не первая строка роли."""
    settings, db, session, user, demo = ctx
    settings.reconciler_stop_check_every = 1
    early = "2104213344721920077"
    sol = await _bot_open_trade(
        session, user.id, "SOL-USDT", entry="123.021", qty="1362.07", entry_id=SOL_ENTRY,
        stop_id=SOL_STOP, take_id=SOL_TAKE, fee="83.781520",
        opened_at=datetime(2026, 9, 27, 14, 15, 30, 300000, tzinfo=UTC),
    )
    session.add(ExecutionOrder(
        account_mode=ExchangeKeyMode.DEMO,
        user_id=user.id, trade_id=sol.id, exchange_order_id=early, symbol="SOL-USDT",
        side=OrderSide.SELL, position_side=TradeSide.LONG, order_type=OrderType.STOP_MARKET,
        role=OrderRole.STOP_LOSS, status=OrderStatus.SUBMITTED,
    ))
    await session.commit()
    demo.positions = [p for p in demo.positions if p["symbol"] != "SOL-USDT"]
    demo.fail_open_orders = True

    await _run_reconciler(settings, db, FakeBot())

    async with db.session() as s:
        rows = list(await s.scalars(select(ExecutionOrder).where(
            ExecutionOrder.trade_id == sol.id, ExecutionOrder.role == OrderRole.STOP_LOSS,
        )))
    by_oid = {r.exchange_order_id: r.status for r in rows}
    assert by_oid == {SOL_STOP: OrderStatus.FILLED, early: OrderStatus.SUBMITTED}


# --- п.1а (24.10): стопы/тейки открытой позиции по openOrders и истории -------------------

LINK_PID = "2104122757805514754"
LINK_STOP_OPEN = live_items("openOrders LINK")[0]   # стоп; [1] — тейк


def _link_hist(oid: str, otype: str, status: str, trigger: str = "0", *,
               created: datetime | None = None) -> dict[str, Any]:
    item = _xrp_hist(oid, otype, status, trigger, created=created)
    item["symbol"] = "LINK-USDT"
    return item


async def _link_rows(db) -> tuple[ExecutionOrder, ExecutionOrder]:  # type: ignore[no-untyped-def]
    rows = await _rows(db, "LINK-USDT")
    return rows[OrderRole.STOP_LOSS], rows[OrderRole.TAKE_PROFIT]


def _stop_gone(demo: LiveDemo) -> None:
    """Владелец снял стоп LINK в BingX: в openOrders остался только тейк."""
    demo.link_open_orders = [o for o in demo.link_open_orders if str(o["orderId"]) != LINK_STOP]


async def _cycle(settings, db, redis=None) -> None:  # type: ignore[no-untyped-def]
    """Новый Reconciler — первый цикл, проверка стопов в нём всегда."""
    await _run_reconciler(settings, db, FakeBot(), redis)


async def test_open_position_stop_cancelled_in_history_is_cancelled_null(ctx) -> None:  # type: ignore[no-untyped-def]
    """Позиция открыта, стоп снят руками: в истории CANCELLED → строка
    CANCELLED без источника (кто снял — неизвестно). Тейк стоит — не тронут.
    Окно истории — от создания строки минус минута."""
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    _stop_gone(demo)
    demo.window_by_created = True
    stop, _take = await _link_rows(db)
    demo.all_orders["LINK-USDT"] = [
        _link_hist(LINK_STOP, "STOP_MARKET", "CANCELLED", created=stop.created_at),
    ]

    await _cycle(settings, db)

    stop, take = await _link_rows(db)
    assert (stop.status, stop.cancel_source) == (OrderStatus.CANCELLED, None)
    assert take.status is OrderStatus.SUBMITTED
    [start] = [w[1] for w in demo.all_orders_windows if w[0] == "LINK-USDT"]
    assert start <= int(stop.created_at.timestamp() * 1000) - 60_000


async def test_open_position_nothing_to_check_no_extra_requests(ctx) -> None:  # type: ignore[no-untyped-def]
    """Стоп и тейк стоят в общем openOrders — ни openOrders по символу, ни
    истории: один снимок на уровни и строки."""
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)

    await _cycle(settings, db)

    stop, take = await _link_rows(db)
    assert {stop.status, take.status} == {OrderStatus.SUBMITTED}
    demo.all_orders["LINK-USDT"] = []
    before = len(demo.calls)
    await _cycle(settings, db)
    # Второй цикл: SOL уже закрыта; LINK — снимок уровней и проверка стопа
    # сделки, без openOrders(LINK) п.1а и без истории.
    assert [w for w in demo.all_orders_windows if w[0] == "LINK-USDT"] == []
    assert demo.calls[before:].count("/openApi/swap/v2/trade/openOrders") == 2


async def test_open_position_absent_everywhere_needs_two_checks(ctx) -> None:  # type: ignore[no-untyped-def]
    """Нет ни в openOrders, ни в полной истории: первая проверка — только
    отметка в Redis с TTL ≈ 3 интервала проверки стопов, вторая подряд —
    CANCELLED/NULL, отметка снята."""
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    _stop_gone(demo)
    demo.all_orders["LINK-USDT"] = []
    redis = FakeRedis()
    stop, _ = await _link_rows(db)
    key = f"recon:gone:{stop.id}"

    await _cycle(settings, db, redis)
    stop, _ = await _link_rows(db)
    assert stop.status is OrderStatus.SUBMITTED
    ttl = 3 * settings.reconciler_stop_check_every * settings.reconciler_interval_seconds
    assert ttl - 5 <= await redis.ttl(key) <= ttl

    await _cycle(settings, db, redis)
    stop, take = await _link_rows(db)
    assert (stop.status, stop.cancel_source) == (OrderStatus.CANCELLED, None)
    assert take.status is OrderStatus.SUBMITTED
    assert await redis.get(key) is None


async def test_open_position_stale_mark_not_counted(ctx) -> None:  # type: ignore[no-untyped-def]
    """Отметка старше 3 интервалов (TTL не успел снять) — не в счёт: это
    снова первая проверка, отметка обновляется."""
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    _stop_gone(demo)
    demo.all_orders["LINK-USDT"] = []
    redis = FakeRedis()
    stop, _ = await _link_rows(db)
    key = f"recon:gone:{stop.id}"
    ttl = 3 * settings.reconciler_stop_check_every * settings.reconciler_interval_seconds
    stale = datetime.now(UTC).timestamp() - ttl - 60
    await redis.set(key, stale)

    await _cycle(settings, db, redis)

    stop, _ = await _link_rows(db)
    assert stop.status is OrderStatus.SUBMITTED
    assert float(await redis.get(key)) > stale


async def test_open_position_mark_cleared_when_order_seen(ctx) -> None:  # type: ignore[no-untyped-def]
    """Подряд: ордер снова виден в openOrders — отметка снимается, следующая
    пропажа начинает отсчёт заново."""
    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    demo.all_orders["LINK-USDT"] = []
    redis = FakeRedis()
    stop, _ = await _link_rows(db)
    key = f"recon:gone:{stop.id}"
    full = list(demo.link_open_orders)
    _stop_gone(demo)
    await _cycle(settings, db, redis)
    assert await redis.get(key) is not None

    demo.link_open_orders = full
    await _cycle(settings, db, redis)
    assert await redis.get(key) is None

    _stop_gone(demo)
    await _cycle(settings, db, redis)
    stop, _ = await _link_rows(db)
    assert stop.status is OrderStatus.SUBMITTED


async def test_open_position_incomplete_history_untouched(ctx) -> None:  # type: ignore[no-untyped-def]
    """Выдача упёрлась в лимит 500 — «нет в истории» не доказано: строка не
    трогается и после двух проверок, отметки нет."""
    from app.exchanges.base import ALL_ORDERS_LIMIT

    settings, db, session, user, demo = ctx
    await _seed_live(session, user.id)
    _stop_gone(demo)
    demo.all_orders["LINK-USDT"] = [
        _link_hist(str(8_000_000 + i), "STOP_MARKET", "CANCELLED") for i in range(ALL_ORDERS_LIMIT)
    ]
    redis = FakeRedis()
    await _cycle(settings, db, redis)
    await _cycle(settings, db, redis)
    stop, _ = await _link_rows(db)
    assert stop.status is OrderStatus.SUBMITTED
    assert await redis.get(f"recon:gone:{stop.id}") is None


async def test_open_position_filled_waits_for_exit_in_journal(ctx) -> None:  # type: ignore[no-untyped-def]
    """Стоп исполнен (дочерний FILLED), а выхода в журнале сделки ещё нет —
    строку не трогаем (частичное закрытие пишет сверка сделки). Выход в
    журнале — FILLED."""
    settings, db, session, user, demo = ctx
    link_id = (await _seed_live(session, user.id))[1]
    _stop_gone(demo)
    child = "2104122758140699999"
    demo.all_orders["LINK-USDT"] = [_link_hist(child, "STOP_MARKET", "FILLED", trigger=LINK_STOP)]

    await _cycle(settings, db)
    stop, _ = await _link_rows(db)
    assert stop.status is OrderStatus.SUBMITTED

    async with db.session() as s:
        trade = await s.scalar(
            select(Trade).where(Trade.id == link_id).options(selectinload(Trade.fills))
        )
        assert trade is not None
        await TradeJournal(TradeRepository(s)).close_trade(
            trade, exit_price=D("14.0"), quantity=D("100"), fee=D("0.1"),
            exit_reason=EXIT_STOP_LOSS, external_fill_id=child,
        )
        await s.commit()
    await _cycle(settings, db)
    stop, _ = await _link_rows(db)
    assert (stop.status, stop.cancel_source) == (OrderStatus.FILLED, None)


async def _position_action_stop(  # type: ignore[no-untyped-def]
    session, user_id: int, oid: str, *, status: PositionActionStatus = PositionActionStatus.DONE,
    mode: ExchangeKeyMode = ExchangeKeyMode.DEMO, error_code: str | None = None,
) -> ExecutionOrder:
    action = PositionAction(
        account_mode=mode, user_id=user_id, symbol="LINK-USDT", side=TradeSide.LONG,
        position_id=LINK_PID, kind=PositionActionKind.MOVE_STOP, status=status,
    )
    session.add(action)
    await session.flush()
    row = ExecutionOrder(
        account_mode=mode, user_id=user_id, position_action_id=action.id,
        exchange_order_id=oid, symbol="LINK-USDT", side=OrderSide.SELL,
        position_side=TradeSide.LONG, order_type=OrderType.STOP_MARKET,
        role=OrderRole.STOP_LOSS, status=OrderStatus.SUBMITTED, error_code=error_code,
    )
    session.add(row)
    await session.commit()
    return row


async def test_open_position_rows_of_manual_position_by_position_id(ctx) -> None:  # type: ignore[no-untyped-def]
    """Позиция без сделки в журнале: строки действий этапа 4 — по positionId
    живой позиции. Исполнен без сделки — FILLED сразу; снят — CANCELLED/NULL.
    Строки идущего действия, MANUAL_RECONCILE и чужого счёта не трогаем."""
    settings, db, session, user, demo = ctx
    demo.link_open_orders = []
    fired = await _position_action_stop(session, user.id, "7401")
    taken = await _position_action_stop(session, user.id, "7402")
    busy = await _position_action_stop(session, user.id, "7403",
                                       status=PositionActionStatus.SUBMITTED)
    manual = await _position_action_stop(session, user.id, "7404", error_code="MANUAL_RECONCILE")
    other = await _position_action_stop(session, user.id, "7405", mode=ExchangeKeyMode.LIVE)
    demo.all_orders["LINK-USDT"] = [
        _link_hist("7499", "STOP_MARKET", "FILLED", trigger="7401"),
        *(_link_hist(oid, "STOP_MARKET", "CANCELLED") for oid in ("7402", "7403", "7404", "7405")),
    ]

    await _cycle(settings, db)

    made = (fired, taken, busy, manual, other)
    got = await _orders(db, [r.id for r in made])
    assert [(got[r.id].status, got[r.id].cancel_source) for r in made] == [
        (OrderStatus.FILLED, None), (OrderStatus.CANCELLED, None),
        (OrderStatus.SUBMITTED, None), (OrderStatus.SUBMITTED, None), (OrderStatus.SUBMITTED, None),
    ]


async def test_open_position_closed_position_rows_not_taken(ctx) -> None:  # type: ignore[no-untyped-def]
    """Позиции нет — это не п.1а (строки закрытой позиции решают пути
    закрытия): строка по positionId закрытой позиции не трогается."""
    settings, db, session, user, demo = ctx
    demo.positions = []
    demo.link_open_orders = []
    row = await _position_action_stop(session, user.id, "7501")
    demo.all_orders["LINK-USDT"] = [_link_hist("7501", "STOP_MARKET", "CANCELLED")]

    await _cycle(settings, db)

    assert (await _orders(db, [row.id]))[row.id].status is OrderStatus.SUBMITTED


async def test_closes_fully_row_older_than_trade_window_left_for_sweep(ctx) -> None:  # type: ignore[no-untyped-def]
    """Уточнение владельца 23.10 (2): окно истории сделки — от входа, а строка
    создана раньше (allOrders отбирает по созданию — её ордера в выдаче нет).
    «Нет в истории» по такому окну не доказано: закрытие сверкой строку не
    трогает, обход закрытых сделок берёт окно от строки и находит отмену в
    допуске от закрытия — EXCHANGE (а не CANCELLED/NULL по неполному окну)."""
    settings, db, session, user, demo = ctx
    demo.window_by_created = True
    opened_live = datetime(2026, 9, 27, 14, 15, 30, 300000, tzinfo=UTC)
    shift = datetime.now(UTC) - timedelta(hours=1) - opened_live
    ms = int(shift.total_seconds() * 1000)
    items = []
    for item in live_items("allOrders SOL"):
        item = dict(item)
        item["time"], item["updateTime"] = int(item["time"]) + ms, int(item["updateTime"]) + ms
        items.append(item)
    closed_ms = max(int(i["updateTime"]) for i in items)
    early = "2104213344721920077"
    early_created = opened_live + shift - timedelta(minutes=10)
    early_item = _xrp_hist(early, "STOP_MARKET", "CANCELLED", created=early_created)
    early_item.update(symbol="SOL-USDT", updateTime=closed_ms + 1000)
    demo.all_orders["SOL-USDT"] = [*items, early_item]
    sol = await _bot_open_trade(
        session, user.id, "SOL-USDT", entry="123.021", qty="1362.07", entry_id=SOL_ENTRY,
        stop_id=SOL_STOP, take_id=SOL_TAKE, fee="83.781520", opened_at=opened_live + shift,
    )
    extra = ExecutionOrder(
        account_mode=ExchangeKeyMode.DEMO,
        user_id=user.id, trade_id=sol.id, exchange_order_id=early, symbol="SOL-USDT",
        side=OrderSide.SELL, position_side=TradeSide.LONG, order_type=OrderType.STOP_MARKET,
        role=OrderRole.STOP_LOSS, status=OrderStatus.SUBMITTED, created_at=early_created,
    )
    session.add(extra)
    await session.commit()
    demo.positions = [p for p in demo.positions if p["symbol"] != "SOL-USDT"]

    await _run_reconciler(settings, db, FakeBot())

    assert (await _trade(db, sol.id)).status is TradeStatus.CLOSED
    got = (await _orders(db, [extra.id]))[extra.id]
    assert (got.status, got.cancel_source) == (OrderStatus.CANCELLED, CancelSource.EXCHANGE)


# --- п.3 (24.10): «📥 В журнал» в сообщении ORPHAN_POSITION -----------------------------


class MarkupBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.markups: list[Any] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent.append(text)
        self.markups.append(reply_markup)


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_orphan_message_has_import_and_positions_buttons(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, _session, _user, _demo = ctx
    bot = MarkupBot()

    await _run_reconciler(settings, db, bot)

    [(text, markup)] = [
        (t, m) for t, m in zip(bot.sent, bot.markups, strict=True) if "сделки в журнале нет" in t
    ]
    assert "LINK-USDT" in text
    assert _buttons(markup) == [
        ("📥 В журнал", "pos:impn:LINK-USDT:L"), ("💼 Позиции", "menu:open"),
    ]


def _orphan_event(
    kind: ReconciliationKind, key: str, symbol: str = "LINK-USDT"
) -> ReconciliationEvent:
    return ReconciliationEvent(user_id=1, symbol=symbol, kind=kind, dedup_key=key, detail="d")


def test_orphan_markup_from_stored_event_and_only_for_orphan() -> None:
    """Событие из базы (переотправка, события до 24.10): сторона — из
    dedup_key, символ сверяется с event.symbol; не разбирается — без кнопок.
    У прочих событий кнопок нет; явная сторона расхождения важнее ключа."""
    from app.workers.reconciler import _orphan_markup

    orphan = ReconciliationKind.ORPHAN_POSITION
    stored = _orphan_markup(_orphan_event(orphan, "orphan:LINK-USDT:SHORT:—"), None)
    assert stored is not None and _buttons(stored)[0][1] == "pos:impn:LINK-USDT:S"
    explicit = _orphan_markup(_orphan_event(orphan, "orphan:LINK-USDT:SHORT:1"), TradeSide.LONG)
    assert explicit is not None and _buttons(explicit)[0][1] == "pos:impn:LINK-USDT:L"
    for bad in ("orphan:XRP-USDT:LONG:1", "orphan:LINK-USDT:FLAT:1", "orphan:LINK-USDT:LONG",
                "stop_missing:LINK-USDT:LONG:1"):
        assert _orphan_markup(_orphan_event(orphan, bad), None) is None, bad
    assert _orphan_markup(
        _orphan_event(ReconciliationKind.STOP_MISSING, "orphan:LINK-USDT:LONG:1"), TradeSide.LONG
    ) is None


async def test_orphan_resend_keeps_buttons(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Переотправка события из базы (Telegram лёг на первой отправке, биржа
    теперь недоступна): кнопки те же — сторона из dedup_key."""
    import app.workers.reconciler as reconciler_module

    settings, db, session, user, _demo = ctx
    now = datetime.now(UTC)
    event = ReconciliationEvent(
        user_id=user.id, symbol="LINK-USDT", kind=ReconciliationKind.ORPHAN_POSITION,
        dedup_key="orphan:LINK-USDT:SHORT:1", detail="d", created_at=now - timedelta(minutes=1),
        notify_text="⚠️ LINK-USDT: сделки в журнале нет", attempts=1,
        last_attempt_at=now - timedelta(minutes=1),
    )
    session.add(event)
    await session.commit()
    monkeypatch.setattr(reconciler_module, "ExchangeFactory", _DownFactory)

    bot = MarkupBot()
    await _run_reconciler(settings, db, bot)

    assert bot.sent == ["⚠️ LINK-USDT: сделки в журнале нет"]
    assert _buttons(bot.markups[0]) == [
        ("📥 В журнал", "pos:impn:LINK-USDT:S"), ("💼 Позиции", "menu:open"),
    ]
    assert (await _event(db, event.id)).notified_at is not None


async def test_other_events_have_no_buttons(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, db, session, user, _demo = ctx
    await _seed_live(session, user.id)
    bot = MarkupBot()

    await _run_reconciler(settings, db, bot)

    others = [m for t, m in zip(bot.sent, bot.markups, strict=True)
              if "сделки в журнале нет" not in t]
    assert any("закрыта по стопу" in t for t in bot.sent)
    assert others and all(m is None for m in others)
