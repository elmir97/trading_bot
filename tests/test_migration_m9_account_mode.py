"""M9 919ae8ad384e (account_mode у position_actions и execution_orders): проверка
«LIVE в истории нет» до разметки DEMO — против настоящей БД, в транзакции с
откатом; текст SQL — ASCII (как у скрипта репетиции: psql на Windows)."""

from __future__ import annotations

import importlib.util
import os
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode, TradeSide, TradeSource
from app.trading.journal import TradeJournal

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "919ae8ad384e_account_mode_actions_orders.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m9_account_mode", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m9 = _load()


def test_sql_is_ascii() -> None:
    for sql in (m9._NO_LIVE, *(s for _, s in m9._FILL)):
        assert sql.isascii(), sql


async def test_live_trade_stops_upgrade(unique_telegram_id) -> None:  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    try:
        async with db.session() as session:
            user = await UserService(
                UserRepository(session), StrategyRepository(session),
                MistakeTypeRepository(session), settings,
            ).get_or_create(telegram_id=unique_telegram_id())
            await TradeJournal(TradeRepository(session)).open_trade(
                user_id=user.id, symbol="XRP-USDT", side=TradeSide.LONG,
                entry_price=Decimal("1.5"), quantity=Decimal(10), stop_loss=Decimal("1.4"),
                source=TradeSource.IMPORTED, account_mode=ExchangeKeyMode.LIVE,
                external_fill_id="m9test", fee=Decimal(0),
            )
            await session.flush()
            with pytest.raises(DBAPIError, match="M9: LIVE rows"):
                await session.execute(text(m9._NO_LIVE))
            await session.rollback()   # ни пользователя, ни сделки не остаётся
    finally:
        await db.dispose()
