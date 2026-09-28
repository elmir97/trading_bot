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
from sqlalchemy import select
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
from tests.bingx_fixtures import live_items
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

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        self.calls.append(path)
        if path == "/openApi/swap/v2/user/positions":
            return _ok(live_items("positions all"))
        if path == "/openApi/swap/v2/trade/allOrders":
            assert params["symbol"] == "SOL-USDT", "история нужна только закрытой SOL"
            return _ok({"orders": live_items("allOrders SOL")})
        if path == "/openApi/swap/v2/trade/openOrders":
            orders = self.link_open_orders if params["symbol"] == "LINK-USDT" else []
            return _ok({"orders": orders})
        cid = params.get("clientOrderID")
        if path == "/openApi/swap/v2/trade/order" and cid in self.unknown_cids:
            return httpx.Response(200, json={"code": 109421, "msg": "order not exist", "data": {}})
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
