"""Сквозной прогон бота через настоящий диспетчер aiogram.

Подменяется только сеть: вместо отправки в Telegram ответы собираются
в список. Всё остальное настоящее — middlewares, роутеры, FSM, база,
расчёты. Это ловит то, что не видят юнит-тесты: битые callback_data,
неверные переходы состояний, необработанные ветки диалога.

Запуск: python -m scripts.simulate_chat

Это библиотека, не готовый сценарий: main() ниже только строит
симулятор. Уборка своих данных — обязанность вызывающего сценария,
как в scripts/smoke_check.py (см. _cleanup_smoke_data + try/finally
в его main()). Любой сценарий, использующий build() с этим USER_ID
(telegram_id=424242), обязан удалить пользователя в finally —
иначе следующий прогон (свой или чужой) найдёт чужие данные и
откажется работать.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
    User as TgUser,
)
from fakeredis.aioredis import FakeRedis

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
        # id последнего сообщения бота (send или edit) — настоящее значение,
        # не отдельный счётчик: execution.py._process_confirm() сверяет его
        # с id на карточке (раздел 5 ТЗ, защита от ответа на устаревшее
        # сообщение), и без него любая проверка "Да" считала бы карточку
        # устаревшей молча (правится только markup, текст и лог не меняются).
        self.last_message_id: int = 0
        self.log: list[tuple[str, str]] = []
        self._next_msg_id = 0

    async def __call__(self, bot: Bot, method: Any, request_timeout: Any = None) -> Any:
        if isinstance(method, SendMessage):
            # sendMessage создаёт НОВОЕ сообщение — новый id.
            self._next_msg_id += 1
            self.last_message_id = self._next_msg_id
            self.last_text = method.text
            self.last_markup = method.reply_markup
            self.log.append(("send", method.text))
            return self._message(method.chat_id, method.text)
        if isinstance(method, EditMessageText):
            # editMessageText правит СУЩЕСТВУЮЩЕЕ — id не меняется.
            self.last_message_id = method.message_id
            self.last_text = method.text
            self.last_markup = method.reply_markup
            self.log.append(("edit", method.text))
            return self._message(method.chat_id, method.text, method.message_id)
        if isinstance(method, EditMessageReplyMarkup):
            # Раздел 5 ТЗ: карточка истекла/обработана — правится только
            # клавиатура (например, на "⌛ Подтверждение просрочено"), текст
            # остаётся тем же. Без этой ветки last_markup был бы устаревшим
            # для любого следующего tap() по той же карточке.
            self.last_message_id = method.message_id
            self.last_markup = method.reply_markup
            self.log.append(("edit_markup", self.last_text))
            return self._message(method.chat_id, self.last_text, method.message_id)
        if isinstance(method, AnswerCallbackQuery) and method.text:
            self.log.append(("alert", method.text))
        return None

    def _message(self, chat_id: int, text: str, message_id: int | None = None) -> Message:
        return Message(
            message_id=message_id or self.last_message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=TgUser(id=BOT_ID, is_bot=True, first_name="Bot"),
            text=text,
        )

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
        return await self.tap_data(data)

    async def tap_data(self, callback_data: str) -> str:
        """То же нажатие, но по готовому callback_data — не по подписи из
        последнего сообщения. Нужно, когда кнопка приходит не через диалог
        с ботом (например, уведомление о сигнале уходит прямым
        bot.send_message из фонового воркера, а не через feed_update), но
        callback_data всё равно известен по формату (см. ExecutionCB)."""
        callback_query = CallbackQuery(
            id=str(self._next_id()),
            from_user=TgUser(id=USER_ID, is_bot=False, first_name="Эльмир"),
            chat_instance="sim",
            data=callback_data,
            message=Message(
                # Настоящий id последнего сообщения бота, не отдельный
                # счётчик — хендлеры, сверяющие message_id с сохранённым
                # состоянием (например, защита от устаревшей карточки
                # подтверждения в execution.py), иначе всегда видели бы
                # чужой id и считали бы любое сообщение устаревшим.
                message_id=self._tg.last_message_id,
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


async def build() -> tuple[Simulator, FakeTelegram, Database, FakeRedis]:
    settings = get_settings()
    db = Database(settings)
    # Локального Redis нет — блокировка от двойного нажатия (build_dispatcher
    # требует клиент с этапа 15.4а) в симуляторе не участвует, FakeRedis
    # достаточно как замены протокола (SET NX / EVAL). Возвращаем клиент
    # наружу: сценарии execution-пути держат тем же RedisLock тот же ключ,
    # что и хендлер, чтобы детерминированно воспроизвести "уже обрабатывается".
    redis = FakeRedis(decode_responses=True)
    dp = build_dispatcher(settings, db, llm_client=None, redis=redis)

    tg = FakeTelegram()

    async def call(self: Bot, method: Any, request_timeout: Any = None) -> Any:
        return await tg(self, method, request_timeout)

    Bot.__call__ = call  # type: ignore[method-assign]

    bot = Bot(token=f"{BOT_ID}:AAFakeTokenForSimulation")
    return Simulator(dp, bot, tg), tg, db, redis


async def main() -> None:
    sim, tg, db, redis = await build()
    print("Симулятор готов. Используй его из сценариев.")
    await redis.aclose()
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
