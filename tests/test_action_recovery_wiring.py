"""B.9: итог восстановления в чате (app/bot/action_result.py) и место прохода
в цикле reconciler — до сверки сделок, после проверки локов."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from aiogram.types import Chat, Message
from sqlalchemy import select

from app.bot import outbox
from app.bot.action_result import KIND, send_action_recovered
from app.core.config import Settings
from app.core.security import SecretCipher
from app.database.models.outgoing_message import OutgoingMessage, OutgoingMeta
from app.database.models.position_action import PositionAction
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.execution.action_recovery import RecoveryNote
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode, PositionActionKind, PositionActionStatus, TradeSide
from app.workers import reconciler as reconciler_module
from app.workers.reconciler import Reconciler
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


class _Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def edit_message_reply_markup(self, **kw: Any) -> None:
        self.calls.append(("markup", kw))

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None) -> Message:
        self.calls.append(("send", text))
        return Message(message_id=900, date=datetime.now(UTC),
                       chat=Chat(id=chat_id, type="private"), text=text)


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        action = PositionAction(
            user_id=user.id, symbol="XRP-USDT", side=TradeSide.LONG,
            account_mode=ExchangeKeyMode.DEMO, kind=PositionActionKind.CLOSE_FULL,
            status=PositionActionStatus.FAILED, card_message_id=55,
        )
        session.add(action)
        await session.commit()
        ids = (user.id, user.telegram_id, action.id)
    yield SimpleNamespace(db=db, settings=settings, user_id=ids[0], chat_id=ids[1],
                          action_id=ids[2])
    async with db.session() as session:
        fresh = await UserRepository(session).get_by_id(ids[0])
        if fresh is not None:
            await cleanup_user(session, fresh)
            await session.commit()
    await db.dispose()


def _note(c: Any) -> RecoveryNote:
    return RecoveryNote(user_id=c.user_id, telegram_id=c.chat_id, action_id=c.action_id,
                        card_message_id=55, text="♻️ После перезапуска бота — итог")


async def test_note_sent_and_card_buttons_removed(ctx) -> None:  # type: ignore[no-untyped-def]
    bot = _Bot()
    await send_action_recovered(bot, ctx.db, _note(ctx))  # type: ignore[arg-type]
    assert bot.calls[0] == ("markup", {"chat_id": ctx.chat_id, "message_id": 55,
                                       "reply_markup": None})
    assert bot.calls[1] == ("send", "♻️ После перезапуска бота — итог")
    async with ctx.db.session() as session:
        [row] = await session.scalars(
            select(OutgoingMessage).where(OutgoingMessage.user_id == ctx.user_id)
        )
    assert (row.kind, row.message_id, row.position_action_id) == (KIND, 900, ctx.action_id)


async def test_final_card_not_touched(ctx) -> None:  # type: ignore[no-untyped-def]
    """Карточка уже стала итогом (например «устарела») — её не правим (A.1)."""
    await outbox.record(ctx.db, OutgoingMeta(user_id=ctx.user_id, kind="ACTION_RESULT"),
                        chat_id=ctx.chat_id, message_id=55, text="⛔ устарела")
    bot = _Bot()
    await send_action_recovered(bot, ctx.db, _note(ctx))  # type: ignore[arg-type]
    assert [name for name, _ in bot.calls] == ["send"]


async def test_reconciler_recovers_actions_before_cycle(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Восстановление — в том же цикле и раньше сверки сделок (orderId закрытия
    должен попасть в строку до разбора выхода)."""
    order: list[str] = []

    async def recover(*args: Any, **kw: Any) -> int:
        order.append("recover")
        return 0

    async def cycle(self: Reconciler) -> None:
        order.append("cycle")

    monkeypatch.setattr(reconciler_module, "recover_actions", recover)
    monkeypatch.setattr(Reconciler, "_run_cycle", cycle)
    from fakeredis.aioredis import FakeRedis

    cipher = SecretCipher(ctx.settings.encryption_key.get_secret_value())
    await Reconciler(_Bot(), ctx.db, ctx.settings, cipher, FakeRedis()).run()  # type: ignore[arg-type]
    assert order == ["recover", "cycle"]


async def test_reconciler_recovery_failure_does_not_stop_cycle(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    order: list[str] = []

    async def recover(*args: Any, **kw: Any) -> int:
        raise RuntimeError("boom")

    async def cycle(self: Reconciler) -> None:
        order.append("cycle")

    monkeypatch.setattr(reconciler_module, "recover_actions", recover)
    monkeypatch.setattr(Reconciler, "_run_cycle", cycle)
    from fakeredis.aioredis import FakeRedis

    cipher = SecretCipher(ctx.settings.encryption_key.get_secret_value())
    rec = Reconciler(_Bot(), ctx.db, ctx.settings, cipher, FakeRedis())  # type: ignore[arg-type]
    await rec.run()
    assert order == ["cycle"] and rec._cycle_errors == 1
