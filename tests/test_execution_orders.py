"""Интеграционные тесты слоя персистентности этапа 15.1.

Только модели, миграция и репозиторий — ничего не исполняется, поэтому
тесты проверяют персистентность и ограничения схемы (идемпотентность по
client_order_id, SET NULL при удалении сигнала/сделки, каскад при
удалении пользователя), а не торговую логику. Против настоящей БД, как
остальные интеграционные тесты в этом наборе.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import Settings
from app.database.models.execution_order import ExecutionOrder
from app.database.models.signal import SignalRecord
from app.database.models.trade import Trade
from app.database.models.user import User
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import (
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalLevel,
    TradeSide,
    TradeSource,
    TradeStatus,
)
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)

D = Decimal

# client_order_id — единственная глобально уникальная колонка в этом файле
# (UNIQUE без привязки к user_id, в отличие от telegram_id ниже). Успешные
# тесты коммитят свои ордера по-настоящему (Database.session() коммитит по
# выходу из `async with`), поэтому голые литералы вроде "tj1" ловили бы
# UniqueViolation при повторном прогоне против той же БД. Префикс на основе
# времени запуска делает их уникальными от прогона к прогону, тем же
# способом, что и telegram_id в фикстурах ctx() по всему проекту.
_RUN_ID = f"{int(datetime.now(UTC).timestamp() * 1_000_000) % 100_000_000:x}"


def _signal(user_id: int, **overrides: object) -> SignalRecord:
    now = datetime.now(UTC)
    fields: dict[str, object] = {
        "user_id": user_id,
        "symbol": "BTC-USDT",
        "timeframe": "4h",
        "level": SignalLevel.READY,
        "setup": "Пробой с ретестом",
        "fingerprint": "fp",
        "detail": "detail",
        "expires_at": now + timedelta(hours=4),
    }
    fields.update(overrides)
    return SignalRecord(**fields)  # type: ignore[arg-type]


def _trade(user_id: int, **overrides: object) -> Trade:
    fields: dict[str, object] = {
        "user_id": user_id,
        "symbol": "BTC-USDT",
        "side": TradeSide.LONG,
        "opened_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return Trade(**fields)  # type: ignore[arg-type]


def _order(user_id: int, client_order_id: str, **overrides: object) -> ExecutionOrder:
    fields: dict[str, object] = {
        "user_id": user_id,
        "client_order_id": f"{_RUN_ID}-{client_order_id}",
        "symbol": "BTC-USDT",
        "side": OrderSide.BUY,
        "position_side": TradeSide.LONG,
        "order_type": OrderType.MARKET,
        "role": OrderRole.ENTRY,
        "quantity": D("0.01"),
    }
    fields.update(overrides)
    return ExecutionOrder(**fields)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        user = await user_service.get_or_create(telegram_id=unique_telegram_id())
        repo = ExecutionOrderRepository(session)
        yield user, session, repo
        await cleanup_user(session, user)
    await db.dispose()


async def test_add_and_get_round_trips_fields(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo = ctx

    order = _order(
        user.id, "tj1", quantity=D("0.014"), price=D("63240.5"),
        order_type=OrderType.TAKE_PROFIT_MARKET, role=OrderRole.TAKE_PROFIT,
        trigger_price=D("65100"),
    )
    repo.add(order)
    await repo.flush()

    fetched = await repo.get(order.id, user.id)
    assert fetched is not None
    assert fetched.quantity == D("0.014")
    assert fetched.price == D("63240.5")
    assert fetched.trigger_price == D("65100")
    assert fetched.order_type is OrderType.TAKE_PROFIT_MARKET
    assert fetched.role is OrderRole.TAKE_PROFIT
    assert fetched.status is OrderStatus.PENDING  # дефолт до отправки на биржу


async def test_get_scoped_to_user(ctx, unique_telegram_id) -> None:  # type: ignore[no-untyped-def]
    user, session, repo = ctx
    other = await UserService(
        UserRepository(session),
        StrategyRepository(session),
        MistakeTypeRepository(session),
        Settings(),  # type: ignore[call-arg]
    ).get_or_create(telegram_id=unique_telegram_id())

    order = _order(other.id, "tj-other")
    repo.add(order)
    await repo.flush()

    assert await repo.get(order.id, user.id) is None
    assert await repo.get(order.id, other.id) is not None

    await cleanup_user(session, other)


async def test_get_by_client_order_id(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo = ctx

    assert await repo.get_by_client_order_id(user.id, "tj-missing") is None

    order = _order(user.id, "tj-lookup")
    repo.add(order)
    await repo.flush()

    found = await repo.get_by_client_order_id(user.id, order.client_order_id)
    assert found is not None
    assert found.id == order.id


async def test_client_order_id_unique_across_users(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 8 ТЗ: client_order_id — ключ идемпотентности, дубль обязан
    упасть на уровне БД, а не полагаться на проверку в коде."""
    from sqlalchemy.exc import IntegrityError

    user, session, repo = ctx

    repo.add(_order(user.id, "tj-dupe"))
    await repo.flush()

    repo.add(_order(user.id, "tj-dupe"))
    with pytest.raises(IntegrityError):
        await repo.flush()
    await session.rollback()


async def test_list_by_signal(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    entry = _order(user.id, "tj-entry", signal_id=signal.id, role=OrderRole.ENTRY)
    stop = _order(user.id, "tj-sl", signal_id=signal.id, role=OrderRole.STOP_LOSS)
    repo.add(entry)
    repo.add(stop)
    repo.add(_order(user.id, "tj-unrelated"))
    await repo.flush()

    orders = await repo.list_by_signal(user.id, signal.id)
    assert {o.client_order_id for o in orders} == {
        entry.client_order_id,
        stop.client_order_id,
    }


async def test_signal_deletion_sets_null_not_cascade(ctx) -> None:  # type: ignore[no-untyped-def]
    """Ордер — факт обращения к бирже, обязан пережить удаление сигнала."""
    user, session, repo = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    order = _order(user.id, "tj-survives-signal", signal_id=signal.id)
    repo.add(order)
    await repo.flush()
    order_id, user_id = order.id, user.id

    await session.delete(signal)
    await session.flush()
    session.expire_all()

    survivor = await repo.get(order_id, user_id)
    assert survivor is not None
    assert survivor.signal_id is None


async def test_trade_deletion_sets_null(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo = ctx
    trade = _trade(user.id)
    session.add(trade)
    await session.flush()

    order = _order(user.id, "tj-survives-trade", trade_id=trade.id)
    repo.add(order)
    await repo.flush()
    order_id, user_id = order.id, user.id

    await session.delete(trade)
    await session.flush()
    session.expire_all()

    survivor = await repo.get(order_id, user_id)
    assert survivor is not None
    assert survivor.trade_id is None


async def test_user_deletion_cascades_to_orders(ctx) -> None:  # type: ignore[no-untyped-def]
    """Проверяем именно FK execution_orders.user_id ON DELETE CASCADE —
    поэтому пользователя удаляем сырым DELETE, а не через ORM-каскады
    User (settings/trading_plan/strategies и т.д.), не относящиеся к делу."""
    user, session, repo = ctx
    order = _order(user.id, "tj-cascade")
    repo.add(order)
    await repo.flush()
    order_id, user_id = order.id, user.id

    await session.execute(delete(User).where(User.id == user_id))
    await session.flush()

    stmt = select(ExecutionOrder).where(ExecutionOrder.id == order_id)
    assert await session.scalar(stmt) is None


async def test_trade_source_signal_execution_round_trips(ctx) -> None:  # type: ignore[no-untyped-def]
    """Правка существующей таблицы из раздела 4 ТЗ: новое значение
    TradeSource и trades.signal_id сохраняются и читаются корректно."""
    user, session, repo = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    trade = _trade(
        user.id, source=TradeSource.SIGNAL_EXECUTION, signal_id=signal.id,
        status=TradeStatus.OPEN,
    )
    session.add(trade)
    await session.flush()
    trade_id, signal_id = trade.id, signal.id
    session.expire_all()

    reloaded = await session.get(Trade, trade_id)
    assert reloaded is not None
    assert reloaded.source is TradeSource.SIGNAL_EXECUTION
    assert reloaded.signal_id == signal_id


async def test_signal_trade_opened_at_defaults_to_none(ctx) -> None:  # type: ignore[no-untyped-def]
    """Guard SIGNAL_ALREADY_USED (этап 15.3) читает это поле: NULL значит
    "сигнал ещё не использован для входа"."""
    user, session, repo = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    assert signal.trade_opened_at is None

    signal.trade_opened_at = datetime.now(UTC)
    await session.flush()
    signal_id = signal.id
    session.expire_all()

    reloaded = await session.get(SignalRecord, signal_id)
    assert reloaded is not None
    assert reloaded.trade_opened_at is not None
