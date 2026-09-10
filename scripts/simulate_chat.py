"""Сквозной прогон бота через настоящий диспетчер aiogram.

Подменяется только сеть: вместо отправки в Telegram ответы собираются
в список. Всё остальное настоящее — middlewares, роутеры, FSM, база,
расчёты. Это ловит то, что не видят юнит-тесты: битые callback_data,
неверные переходы состояний, необработанные ветки диалога.

Запуск: python -m scripts.simulate_chat
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
    User as TgUser,
)

from app.core.config import get_settings
from app.database.session import Database
from app.main import build_dispatcher

CHAT_ID = 424242
USER_ID = 424242
BOT_ID = 1234567890

_TAGS = re.compile(r"</?[bi]>")


class FakeTelegram:
    """Перехватывает исходящие вызовы и хранит последнее сообщение."""

    def __init__(self) -> None:
        self.last_text: str = ""
        self.last_markup: InlineKeyboardMarkup | None = None
        self.log: list[tuple[str, str]] = []

    async def __call__(self, bot: Bot, method: Any, request_timeout: Any = None) -> Any:
        if isinstance(method, SendMessage | EditMessageText):
            self.last_text = method.text
            self.last_markup = method.reply_markup
            kind = "send" if isinstance(method, SendMessage) else "edit"
            self.log.append((kind, method.text))
        elif isinstance(method, AnswerCallbackQuery):
            if method.text:
                self.log.append(("alert", method.text))
        return None

    def buttons(self) -> dict[str, str]:
        """Текст кнопки → callback_data последнего сообщения."""
        if self.last_markup is None:
            return {}
        return {
            b.text: b.callback_data or ""
            for row in self.last_markup.inline_keyboard
            for b in row
            if b.callback_data
        }


def plain(text: str) -> str:
    return _TAGS.sub("", text)


class Simulator:
    def __init__(self, dp: Any, bot: Bot, tg: FakeTelegram) -> None:
        self._dp = dp
        self._bot = bot
        self._tg = tg
        self._msg_id = 0

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _message(self, text: str) -> Message:
        return Message(
            message_id=self._next_id(),
            date=datetime.now(UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=TgUser(id=USER_ID, is_bot=False, first_name="Эльмир"),
            text=text,
        )

    async def send(self, text: str) -> str:
        await self._dp.feed_update(
            bot=self._bot,
            update=Update(update_id=self._next_id(), message=self._message(text)),
        )
        return plain(self._tg.last_text)

    async def tap(self, button_text: str) -> str:
        """Нажатие кнопки по её подписи, как это делает пользователь."""
        buttons = self._tg.buttons()
        data = None
        for label, callback in buttons.items():
            if button_text.lower() in label.lower():
                data = callback
                break
        if data is None:
            raise AssertionError(
                f"Кнопки «{button_text}» нет. Доступны: {list(buttons)}"
            )

        callback_query = CallbackQuery(
            id=str(self._next_id()),
            from_user=TgUser(id=USER_ID, is_bot=False, first_name="Эльмир"),
            chat_instance="sim",
            data=data,
            message=Message(
                message_id=self._next_id(),
                date=datetime.now(UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                from_user=TgUser(id=BOT_ID, is_bot=True, first_name="Bot"),
                text=self._tg.last_text or "…",
            ),
        )
        await self._dp.feed_update(
            bot=self._bot,
            update=Update(update_id=self._next_id(), callback_query=callback_query),
        )
        return plain(self._tg.last_text)

    def available_buttons(self) -> list[str]:
        return list(self._tg.buttons())


async def build() -> tuple[Simulator, FakeTelegram, Database]:
    settings = get_settings()
    db = Database(settings)
    dp = build_dispatcher(settings, db, llm_client=None)

    tg = FakeTelegram()

    async def call(self: Bot, method: Any, request_timeout: Any = None) -> Any:
        return await tg(self, method, request_timeout)

    Bot.__call__ = call  # type: ignore[method-assign]

    bot = Bot(token=f"{BOT_ID}:AAFakeTokenForSimulation")
    return Simulator(dp, bot, tg), tg, db


async def main() -> None:
    sim, tg, db = await build()
    print("Симулятор готов. Используй его из сценариев.")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
