"""Регрессии, найденные сквозным прогоном бота.

Каждый тест здесь соответствует багу, который проявлялся только при
реальном прохождении диалога и не ловился юнит-тестами.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from aiogram.dispatcher.event.bases import SkipHandler

from app.bot.keyboards.trade import plural_trades, trade_card
from app.bot.middlewares.errors import ErrorMiddleware
from app.core.config import Settings
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import TradeSide
from app.trading.journal import TradeJournal

D = Decimal


class TestErrorMiddlewarePassesControlFlow:
    """SkipHandler — управляющее исключение aiogram, а не ошибка.

    Когда middleware ловил его как обычное исключение, пользователь
    получал «внутреннюю ошибку» на любую команду, включая /start.
    """

    async def test_skip_handler_is_reraised(self) -> None:
        async def handler(event, data):  # type: ignore[no-untyped-def]
            raise SkipHandler

        with pytest.raises(SkipHandler):
            await ErrorMiddleware()(handler, object(), {})  # type: ignore[arg-type]

    async def test_real_errors_are_still_swallowed(self) -> None:
        async def handler(event, data):  # type: ignore[no-untyped-def]
            raise ValueError("настоящая ошибка")

        # Обычное исключение не должно всплывать наружу: пользователь
        # получает понятное сообщение, traceback уходит в лог.
        result = await ErrorMiddleware()(handler, object(), {})  # type: ignore[arg-type]
        assert result is None


class TestPluralAgreement:
    @pytest.mark.parametrize(
        ("count", "word"),
        [(1, "сделка"), (2, "сделки"), (5, "сделок"),
         (11, "сделок"), (21, "сделка"), (104, "сделки")],
    )
    def test_plural_agreement(self, count: int, word: str) -> None:
        assert plural_trades(count) == word


pytestmark_db = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)


@pytest_asyncio.fixture
async def journal_ctx():  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        tg = 300_000 + int(datetime.now(UTC).timestamp() * 1_000_000) % 90_000
        user = await svc.get_or_create(telegram_id=tg)
        repo = TradeRepository(session)
        yield user, TradeJournal(repo), session
    await db.dispose()


@pytestmark_db
async def test_trade_card_renders_right_after_creation(journal_ctx) -> None:  # type: ignore[no-untyped-def]
    """Регрессия: карточка новой сделки роняла бота.

    trade_card обращается к trade.mistakes; у только что сохранённой
    сделки коллекция была не загружена, и ленивая подгрузка падала
    с MissingGreenlet. Добавление сделки не работало вообще.
    """
    user, journal, _ = journal_ctx

    trade = await journal.open_trade(
        user_id=user.id,
        symbol="BTC-USDT",
        side=TradeSide.LONG,
        entry_price=D("100000"),
        quantity=D("0.1"),
        stop_loss=D("98000"),
        take_profit=D("104000"),
        leverage=10,
        account_balance=D("10000"),
    )

    card = trade_card(trade)
    assert "BTC-USDT" in card
    assert "Риск: 2%" in card          # формат без хвоста нулей
    assert "Плановый RR: 1:2" in card
    assert "Объём: 0.1" in card
