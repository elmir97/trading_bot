"""Журнал исходящих итоговых сообщений (M6 dd2f284754a7, очередь A.2) против
настоящей БД: запись, правка того же сообщения, сбой записи не роняет
отправку, send()/edit() пишут message_id отправленного."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.bot import outbox
from app.core.config import Settings
from app.database.models.outgoing_message import OutgoingMessage, OutgoingMeta
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.workers.notifier import Delivery
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


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


async def _rows(ctx: Any) -> list[OutgoingMessage]:
    async with ctx.db.session() as session:
        return list(await session.scalars(
            select(OutgoingMessage).where(OutgoingMessage.user_id == ctx.user_id)
            .order_by(OutgoingMessage.id)
        ))


def _message(chat_id: int, message_id: int, text: str = "") -> Message:
    return Message(
        message_id=message_id, date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"), text=text,
    )


async def test_record_new_then_edit_keeps_history(ctx) -> None:  # type: ignore[no-untyped-def]
    meta = OutgoingMeta(user_id=ctx.user_id, kind="OPEN_WORKING", trade_opening_id=None)
    await outbox.record(ctx.db, meta, chat_id=ctx.chat_id, message_id=485, text="⏳ Лимит")
    [row] = await _rows(ctx)
    assert (row.kind, row.text, row.edits, row.edited_at) == ("OPEN_WORKING", "⏳ Лимит", [], None)

    done = OutgoingMeta(user_id=ctx.user_id, kind="OPEN_EXPIRED")
    await outbox.record(ctx.db, done, chat_id=ctx.chat_id, message_id=485, text="⌛ Срок вышел")
    [row] = await _rows(ctx)
    assert row.kind == "OPEN_EXPIRED" and row.text == "⌛ Срок вышел"
    assert row.edited_at is not None
    assert [(e["kind"], e["text"]) for e in row.edits] == [("OPEN_WORKING", "⏳ Лимит")]


async def test_same_message_id_other_chat_is_separate(ctx) -> None:  # type: ignore[no-untyped-def]
    meta = OutgoingMeta(user_id=ctx.user_id, kind="OPEN_DONE")
    await outbox.record(ctx.db, meta, chat_id=ctx.chat_id, message_id=1, text="a")
    await outbox.record(ctx.db, meta, chat_id=ctx.chat_id + 1, message_id=1, text="b")
    assert [r.text for r in await _rows(ctx)] == ["a", "b"]


async def test_unique_chat_message(ctx) -> None:  # type: ignore[no-untyped-def]
    async with ctx.db.session() as session:
        for _ in range(2):
            session.add(OutgoingMessage(
                user_id=ctx.user_id, chat_id=ctx.chat_id, message_id=9, kind="X", text="t",
            ))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_record_failure_is_logged_not_raised(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    meta = OutgoingMeta(user_id=-1, kind="OPEN_DONE")   # FK на users не пройдёт
    with caplog.at_level(logging.ERROR):
        await outbox.record(ctx.db, meta, chat_id=ctx.chat_id, message_id=2, text="x")
    assert "не записано в журнал" in caplog.text
    assert await _rows(ctx) == []


class _Bot:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result, self.error, self.calls = result, error, 0

    async def send_message(self, chat_id: int, text: str, **_: Any) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


async def test_send_records_sent_message(ctx) -> None:  # type: ignore[no-untyped-def]
    bot = _Bot(_message(ctx.chat_id, 777, "✅ Открыто"))
    meta = OutgoingMeta(user_id=ctx.user_id, kind="OPEN_DONE")
    delivery = await outbox.send(bot, ctx.db, meta, ctx.chat_id, "✅ Открыто")  # type: ignore[arg-type]
    assert delivery is Delivery.DELIVERED
    [row] = await _rows(ctx)
    assert (row.message_id, row.chat_id, row.text) == (777, ctx.chat_id, "✅ Открыто")


async def test_send_failure_records_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    error = TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="down")
    meta = OutgoingMeta(user_id=ctx.user_id, kind="OPEN_DONE")
    delivery = await outbox.send(_Bot(error=error), ctx.db, meta, ctx.chat_id, "x")  # type: ignore[arg-type]
    assert delivery is Delivery.FAILED
    assert await _rows(ctx) == []


async def test_edit_records_edited_message(ctx) -> None:  # type: ignore[no-untyped-def]
    edited: list[str] = []

    async def edit_text(text: str, **_: Any) -> None:
        edited.append(text)

    message = SimpleNamespace(
        photo=None, chat=SimpleNamespace(id=ctx.chat_id), message_id=485, edit_text=edit_text,
    )
    meta = OutgoingMeta(user_id=ctx.user_id, kind="OPEN_ALARM", trade_opening_id=None)
    await outbox.edit(message, ctx.db, meta, "🚨🚨 БЕЗ СТОПА")  # type: ignore[arg-type]
    assert edited == ["🚨🚨 БЕЗ СТОПА"]
    [row] = await _rows(ctx)
    assert (row.message_id, row.kind) == (485, "OPEN_ALARM")


async def test_openings_worker_notify_records(ctx) -> None:  # type: ignore[no-untyped-def]
    """Цикл восстановления открытий шлёт итог через журнал (meta задан)."""
    from app.trading.enums import TradeSide
    from app.workers.openings import OpeningsWorker

    bot = _Bot(_message(ctx.chat_id, 901, "✅ Открыто"))
    worker = OpeningsWorker(
        bot, ctx.db, Settings(), None, None, factory=object(),  # type: ignore[arg-type, call-arg]
    )
    meta = OutgoingMeta(user_id=ctx.user_id, kind="OPEN_DONE")
    await worker.notify(ctx.chat_id, "✅ Открыто", ("XRP-USDT", TradeSide.LONG), meta=meta)
    await worker.notify(ctx.chat_id, "без журнала", None)   # meta нет — только отправка
    assert bot.calls == 2
    [row] = await _rows(ctx)
    assert (row.message_id, row.kind) == (901, "OPEN_DONE")


async def test_finals_and_mark(ctx) -> None:  # type: ignore[no-untyped-def]
    """A.1 (10.10.2026): finals() — какие сообщения итоговые; mark() — отметка
    в edits без смены текста и вида."""
    meta = OutgoingMeta(user_id=ctx.user_id, kind="OPEN_DONE")
    await outbox.record(ctx.db, meta, chat_id=ctx.chat_id, message_id=503, text="✅ Открыто")
    assert await outbox.finals(ctx.db, ctx.chat_id, [502, 503, 504]) == {503}
    assert await outbox.finals(ctx.db, ctx.chat_id + 1, [503]) == set()
    assert await outbox.finals(ctx.db, ctx.chat_id, []) == set()

    await outbox.mark(ctx.db, ctx.chat_id, [503, 504], "delete_skipped", "wizard_trail")
    [row] = await _rows(ctx)
    assert (row.kind, row.text, row.edited_at) == ("OPEN_DONE", "✅ Открыто", None)
    [entry] = row.edits
    assert (entry["event"], entry["by"]) == ("delete_skipped", "wizard_trail")
    assert "at" in entry and "text" not in entry


async def test_edit_changes_final_on_purpose(ctx) -> None:  # type: ignore[no-untyped-def]
    """outbox.edit — намеренная правка итога (⏳ → «Лимит отменён»): защита
    навигации (A.1) её не перехватывает — сообщение правится, не дублируется."""
    from unittest.mock import AsyncMock, MagicMock

    from app.bot.messaging import reset_final, set_final

    message = MagicMock(spec=Message)
    message.chat = SimpleNamespace(id=ctx.chat_id)
    message.message_id = 485
    message.photo = None
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    meta = OutgoingMeta(user_id=ctx.user_id, kind="OPEN_CANCELLED")
    token = set_final(ctx.chat_id, 485)
    try:
        assert await outbox.edit(message, ctx.db, meta, "✖️ Лимит отменён") is None
    finally:
        reset_final(token)
    message.edit_text.assert_awaited_once()
    message.answer.assert_not_awaited()
    [row] = await _rows(ctx)
    assert (row.message_id, row.text) == (485, "✖️ Лимит отменён")
