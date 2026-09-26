"""Коммит сканера после каждого слота — против настоящей БД.

Кнопка «Открыть сделку» и «Да» приходят из отдельной сессии хендлера, пока
проход сканера ещё идёт. Раньше коммит был один на весь run(): снимок
первого уведомления был не виден («Уведомление устарело»), а строка слота,
переписанная UPDATE-ом сканера, оставалась заблокированной до конца прохода.

Проба выполняется изнутри заглушки send_notification при отправке ВТОРОГО
уведомления прохода — то есть ровно «до конца прохода» — и смотрит на
ПЕРВОЕ. Сеть (BingX, Telegram) не участвует.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import selectinload

from app.analysis.signals import Signal, SignalCondition
from app.bot.handlers import execution
from app.bot.keyboards.execution import ExecutionCB
from app.core.config import Settings
from app.database.models.user import User
from app.database.repositories.signal import SignalRepository
from app.database.repositories.signal_notification import SignalNotificationRepository
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import SignalDirection
from app.workers import scanner as scanner_module
from app.workers.scanner import SetupScanner

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)

SYMBOLS = ["BTC-USDT", "ETH-USDT"]
BOT_ID = 7000000001


def _ready_signal(symbol: str, entry_low: str) -> Signal:
    low = Decimal(entry_low)
    return Signal(
        symbol=symbol,
        timeframe="4h",
        direction=SignalDirection.LONG,
        setup="Пробой с ретестом",
        entry_zone_low=low,
        entry_zone_high=low + Decimal("1"),
        stop_loss=low - Decimal("2"),
        take_profit_1=low + Decimal("6"),
        risk_reward=Decimal("2.5"),
        confidence=7,
        conditions=[SignalCondition("Фильтр EMA200", True, "")],
    )


async def _load_user(session, user_id: int) -> User:  # type: ignore[no-untyped-def]
    return (
        await session.scalars(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.trading_plan), selectinload(User.settings))
        )
    ).one()


def _notification_id(reply_markup) -> int:  # type: ignore[no-untyped-def]
    data = reply_markup.inline_keyboard[0][0].callback_data
    assert data.startswith(ExecutionCB.OPEN)
    return int(data.removeprefix(ExecutionCB.OPEN))


@pytest_asyncio.fixture
async def ctx(monkeypatch, unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)

    # Пользователь закоммичен: сканер и проба ходят в своих сессиях.
    telegram_id = unique_telegram_id()
    async with db.session() as session:
        user = await UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        ).get_or_create(telegram_id=telegram_id)
        user.trading_plan.allowed_symbols = list(SYMBOLS)
        user.settings.notifications = {
            **user.settings.notifications,
            "setup_ready": True,
            "setup_forming": True,
            "setup_charts": False,
        }
        user_id = user.id

    async def fake_list_active_with_plan(self):  # type: ignore[no-untyped-def]
        return [await _load_user(self.session, user_id)]

    monkeypatch.setattr(UserRepository, "list_active_with_plan", fake_list_active_with_plan)
    monkeypatch.setattr(scanner_module, "SCAN_TIMEFRAMES", ("4h",))

    calls: list[object] = []

    async def fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call)
    bot = Bot(token=f"{BOT_ID}:AAFakeTokenForScannerCommitTests")

    scanner = SetupScanner(bot, db, settings)
    # Вход по символу; тест меняет его между проходами, чтобы сдвинуть fingerprint.
    entries = {"BTC-USDT": "100", "ETH-USDT": "200"}
    # evaluate() получает только контекст — символ запоминает build_context.
    current: dict[str, str] = {}

    async def fake_build_context(symbol, timeframe):  # type: ignore[no-untyped-def]
        current["symbol"] = symbol
        return object()  # не None — иначе сканер возьмёт wait_signal

    async def fake_symbol_info(symbol):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(scanner._engine, "build_context", fake_build_context)
    monkeypatch.setattr(
        scanner._engine, "evaluate",
        lambda context_: _ready_signal(current["symbol"], entries[current["symbol"]]),
    )
    monkeypatch.setattr(scanner._engine, "get_symbol_info", fake_symbol_info)

    yield db, scanner, bot, calls, user_id, entries

    await bot.session.close()
    async with db.session() as session:
        await session.delete(await _load_user(session, user_id))
    await db.dispose()


def _install_send_probe(monkeypatch, probe):  # type: ignore[no-untyped-def]
    """send_notification сканера: запоминает id снимков; на втором вызове
    прохода запускает probe(первый id) и пишет её результат."""
    sent: list[int] = []
    results: list[object] = []

    async def fake_send(bot, telegram_id, text_, *, reply_markup=None):  # type: ignore[no-untyped-def]
        if sent:
            results.append(await probe(sent[0]))
        sent.append(_notification_id(reply_markup))
        return True

    monkeypatch.setattr(scanner_module, "send_notification", fake_send)
    return sent, results


async def test_card_opens_when_button_pressed_before_pass_ends(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db, scanner, bot, calls, user_id, _ = ctx
    built: list[int] = []

    async def fake_build_quote(session, user, notification, slot, plan, settings, cipher, **kw):  # type: ignore[no-untyped-def]
        built.append(notification.id)
        return object()

    async def fake_send_result(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(execution, "_build_quote", fake_build_quote)
    monkeypatch.setattr(execution, "_send_result", fake_send_result)

    async def press_open(notification_id: int) -> bool:
        before = len(calls)
        async with db.session() as session:
            user = await _load_user(session, user_id)
            user_tg = user.telegram_id
            # message привязывается к bot отдельно: as_() на CallbackQuery
            # не доходит до вложенного Message, а «устарело» — это
            # callback.message.answer().
            message = Message(
                message_id=1, date=datetime.now(UTC),
                chat=Chat(id=user_tg, type="private"), text="notification",
            ).as_(bot)
            callback = CallbackQuery(
                id="1", from_user=TgUser(id=user_tg, is_bot=False, first_name="Tester"),
                chat_instance="ci", data=f"{ExecutionCB.OPEN}{notification_id}",
                message=message,
            ).as_(bot)
            await execution.open_confirmation(
                callback, session, user, scanner._settings, None, db  # type: ignore[arg-type]
            )
        stale = [
            m for m in calls[before:]
            if isinstance(m, SendMessage) and m.text == execution.STALE_NOTIFICATION_TEXT
        ]
        return not stale

    sent, results = _install_send_probe(monkeypatch, press_open)
    await scanner.run()

    assert len(sent) == 2
    assert results == [True]  # не «устарело»
    assert built == [sent[0]]  # карточка строилась по первому уведомлению


async def test_slot_lock_released_before_pass_ends(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """«Да» по живому уведомлению из прошлого прохода: сканер переписывает
    слот (продлевает expires_at) без нового уведомления, затем шлёт
    уведомление по другому слоту. Блокировка первого слота к этому моменту
    должна быть уже снята."""
    db, scanner, bot, calls, user_id, entries = ctx

    async def no_probe(_id: int) -> None:
        return None

    # Проход 1: оба слота новые, оба уведомления.
    first_pass, _ = _install_send_probe(monkeypatch, no_probe)
    await scanner.run()
    assert len(first_pass) == 2
    btc_notification = first_pass[0]

    async with db.session() as session:
        notification = await SignalNotificationRepository(session).get(btc_notification, user_id)
        assert notification is not None
        btc_slot_id = notification.signal_id

    async def lock_slot(_ignored: int) -> bool:
        async with db.session() as session:
            await session.execute(text("SET LOCAL lock_timeout = '1s'"))
            try:
                slot = await SignalRepository(session).get_for_update(btc_slot_id, user_id)
            except DBAPIError:
                return False
            return slot is not None

    # Проход 2: BTC тот же (UPDATE слота, без уведомления), ETH сместился —
    # новое уведомление, единственное в проходе. Проба — при его отправке.
    entries["ETH-USDT"] = "210"
    results: list[bool] = []

    async def fake_send(bot_, telegram_id, text_, *, reply_markup=None):  # type: ignore[no-untyped-def]
        results.append(await lock_slot(0))
        return True

    monkeypatch.setattr(scanner_module, "send_notification", fake_send)
    await scanner.run()

    assert results == [True]  # блокировку слота BTC взяли сразу
