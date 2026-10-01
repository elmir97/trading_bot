"""Старые кнопки сигналов «exn:…» после удаления сигналов (02.10.2026):
честный ответ и снятые кнопки, без записи в базу и без биржи."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup
from aiogram.types import Chat, Message

from app.bot.handlers.signals_retired import (
    RETIRED_PREFIX,
    RETIRED_TEXT,
    router,
    signal_button_retired,
)


class _Callback:
    def __init__(self, message: object) -> None:
        self.message = message
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def _message() -> Message:
    return Message(
        message_id=7, date=datetime.now(UTC), chat=Chat(id=1, type="private"), text="старое"
    )


async def test_answers_alert_and_drops_keyboard(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dropped: list[object] = []

    async def fake_edit(self: Message, reply_markup: object = None) -> None:
        dropped.append(reply_markup)

    monkeypatch.setattr(Message, "edit_reply_markup", fake_edit)
    callback = _Callback(_message())
    await signal_button_retired(callback)  # type: ignore[arg-type]
    assert callback.answers == [(RETIRED_TEXT, True)]
    assert dropped == [None]


async def test_telegram_error_on_edit_does_not_block_answer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Сообщение удалено или уже без кнопок — ответ всё равно уходит."""

    async def failing_edit(self: Message, reply_markup: object = None) -> None:
        raise TelegramBadRequest(
            method=EditMessageReplyMarkup(chat_id=1, message_id=7), message="not modified"
        )

    monkeypatch.setattr(Message, "edit_reply_markup", failing_edit)
    callback = _Callback(_message())
    await signal_button_retired(callback)  # type: ignore[arg-type]
    assert callback.answers == [(RETIRED_TEXT, True)]


async def test_inaccessible_message_still_answers() -> None:
    callback = _Callback(None)
    await signal_button_retired(callback)  # type: ignore[arg-type]
    assert callback.answers == [(RETIRED_TEXT, True)]


def test_prefix_matches_old_buttons() -> None:
    for data in ("exn:open:1234567890", "exn:yes:1", "exn:no:1"):
        assert data.startswith(RETIRED_PREFIX)
    assert router.name == "signals_retired"
