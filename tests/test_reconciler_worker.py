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
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
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
        self.unknown_cids: set[str] = set()
        # GET /trade/order по clientOrderID: cid → живой ответ ордера.
        self.orders_by_cid: dict[str, dict[str, Any]] = {}
        # По умолчанию — состояние 27.09 (LINK открыта, SOL закрыта стопом);
        # сценарий 29.09 подменяет позиции и историю LINK.
        self.positions: list[dict[str, Any]] = live_items("positions all")
        self.all_orders: dict[str, list[dict[str, Any]]] = {
            "SOL-USDT": live_items("allOrders SOL"),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        self.calls.append(path)
        if path == "/openApi/swap/v2/user/positions":
            return _ok(self.positions)
        if path == "/openApi/swap/v2/trade/allOrders":
            assert params["symbol"] in self.all_orders, "история нужна только закрытым"
            return _ok({"orders": self.all_orders[params["symbol"]]})
        if path == "/openApi/swap/v2/trade/openOrders":
            orders = self.link_open_orders if params["symbol"] == "LINK-USDT" else []
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
    assert rows[OrderRole.TAKE_PROFIT].status is OrderStatus.CANCELED

    link = await _trade(db, link_id)
    assert link.status is TradeStatus.OPEN
    assert len(bot.sent) == 1 and "закрыта по стопу" in bot.sent[0]
    assert "#" + str(sol_id) in bot.sent[0]


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
    assert rows[OrderRole.STOP_LOSS].status is OrderStatus.CANCELED
    assert rows[OrderRole.TAKE_PROFIT].status is OrderStatus.CANCELED

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
