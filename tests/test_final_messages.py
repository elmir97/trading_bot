"""A.1 (10.10.2026, баг №1): итоговое сообщение (журнал исходящих) навигация
не правит — экран новым сообщением; outbox.edit правит намеренно. C.1:
«message is not modified» — не ошибка. FinalMessageMiddleware — против
настоящей БД."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, Message

from app.bot import outbox
from app.bot.messaging import edit_or_replace, is_final, reset_final, set_final
from app.bot.middlewares.final_messages import FinalMessageMiddleware
from app.core.config import Settings
from app.database.models.outgoing_message import OutgoingMeta
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from tests.conftest import cleanup_user

TG = 777


def _message(message_id: int, chat_id: int = TG) -> MagicMock:
    message = MagicMock(spec=Message)
    message.chat = SimpleNamespace(id=chat_id)
    message.message_id = message_id
    message.photo = None
    message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=message_id + 100))
    return message


async def test_final_message_not_edited_screen_is_new() -> None:
    message = _message(503)
    token = set_final(TG, 503)
    try:
        shown = await edit_or_replace(message, "Меню", None)
    finally:
        reset_final(token)
    message.edit_text.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert shown is not None and shown.message_id == 603


async def test_other_message_edited_in_place() -> None:
    message = _message(504)
    token = set_final(TG, 503)
    try:
        assert await edit_or_replace(message, "Меню", None) is None
    finally:
        reset_final(token)
    message.edit_text.assert_awaited_once()
    message.answer.assert_not_awaited()


async def test_same_id_other_chat_not_final() -> None:
    token = set_final(TG, 503)
    try:
        assert not is_final(_message(503, chat_id=TG + 1))
        assert is_final(_message(503))
    finally:
        reset_final(token)
    assert not is_final(_message(503))


async def test_allow_final_edits() -> None:
    """outbox.edit — намеренная правка итога (⏳ → «Лимит отменён»)."""
    message = _message(503)
    token = set_final(TG, 503)
    try:
        await edit_or_replace(message, "✖️ Лимит отменён", None, allow_final=True)
    finally:
        reset_final(token)
    message.edit_text.assert_awaited_once()


async def test_not_modified_is_not_error() -> None:
    """C.1: «Позиции» с тем же содержимым — без ERROR и Traceback."""
    message = _message(504)
    message.edit_text.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="Bad Request: message is not modified: specified new message content "
                "and reply markup are exactly the same",
    )
    assert await edit_or_replace(message, "Позиции", None) is None


async def test_other_bad_request_raised() -> None:
    message = _message(504)
    message.edit_text.side_effect = TelegramBadRequest(
        method=MagicMock(), message="Bad Request: message to edit not found"
    )
    with pytest.raises(TelegramBadRequest):
        await edit_or_replace(message, "Позиции", None)


# --- middleware против настоящей БД ---------------------------------------------------

needs_db = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        await session.commit()
        user_id, chat_id = user.id, user.telegram_id
    yield SimpleNamespace(db=db, user_id=user_id, chat_id=chat_id)
    async with db.session() as session:
        fresh = await UserRepository(session).get_by_id(user_id)
        if fresh is not None:
            await cleanup_user(session, fresh)
            await session.commit()
    await db.dispose()


def _callback(chat_id: int, message_id: int) -> CallbackQuery:
    from aiogram.types import User as TgUser

    return CallbackQuery(
        id="1", chat_instance="x", data="menu:main",
        from_user=TgUser(id=chat_id, is_bot=False, first_name="u"),
        message=Message(
            message_id=message_id, date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"), text="итог",
        ),
    )


async def _seen(middleware: FinalMessageMiddleware, event: Any) -> bool:
    seen: list[bool] = []

    async def handler(event: Any, data: Any) -> None:
        seen.append(is_final(event.message))

    await middleware(handler, event, {})
    return seen[0]


@needs_db
async def test_middleware_marks_only_journal_messages(ctx) -> None:  # type: ignore[no-untyped-def]
    meta = OutgoingMeta(user_id=ctx.user_id, kind="ACTION_RESULT")
    await outbox.record(ctx.db, meta, chat_id=ctx.chat_id, message_id=503, text="✅ Закрыто")
    middleware = FinalMessageMiddleware(ctx.db)
    assert await _seen(middleware, _callback(ctx.chat_id, 503)) is True
    assert await _seen(middleware, _callback(ctx.chat_id, 504)) is False
    assert not is_final(_callback(ctx.chat_id, 503).message)  # type: ignore[arg-type]


async def test_middleware_unreadable_journal_counts_as_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def finals(db: Any, chat_id: int, ids: Any) -> set[int]:
        raise RuntimeError("db down")

    monkeypatch.setattr(outbox, "finals", finals)
    assert await _seen(FinalMessageMiddleware(MagicMock()), _callback(TG, 503)) is True


async def test_action_card_under_final_attached_to_new_message() -> None:
    """Карточка действия этапа 4 под итогом — новым сообщением, и «Да»
    сверяет id нового (иначе карточка считалась бы устаревшей)."""
    from app.bot.handlers import position_actions
    from app.trading.enums import PositionActionKind, TradeSide

    action = SimpleNamespace(id=41)
    service = MagicMock()
    service.open_card = AsyncMock(return_value=SimpleNamespace(
        action=action, refused=False, risk_increase=False, text="Карточка"
    ))
    service.attach_message = AsyncMock()
    message = _message(503)
    token = set_final(TG, 503)
    try:
        await position_actions._show_card(
            message, service, PositionActionKind.CLOSE_PARTIAL, {}, "XRP-USDT",
            TradeSide.LONG, edit=True,
        )
    finally:
        reset_final(token)
    message.edit_text.assert_not_awaited()
    service.attach_message.assert_awaited_once_with(action, 603)
