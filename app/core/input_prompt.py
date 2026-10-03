"""Числовой ввод в чате: вопрос с ForceReply и «пользователь сейчас вводит».

Вопрос бота (app/bot/prompts.ask_number) — отдельное сообщение с ForceReply:
клиент Telegram держит «ответ на» этот вопрос, даже когда сверху приходят
фоновые уведомления (03.10, жалоба владельца: вопрос уезжал вверх). Его id,
время и подсказка лежат в данных FSM под ключами ниже.

InputGate — для фоновых задач: пока пользователь вводит число (вопрос задан
не раньше INPUT_DEFER назад), некритичные уведомления откладываются (сверка
«позиция не в журнале», «сделка открыта в журнале, позиции нет», дневная
сводка, сводка исполнения). Критичные (приближение к SL/TP, закрытие позиции,
позиция без стопа, дневной лимит) не откладываются никогда. Хранилище FSM —
MemoryStorage того же процесса (app/main.build_dispatcher); нет хранилища или
сбой чтения — не откладываем: молча терять уведомления нельзя.

Здесь, а не в app/bot: воркеры app.bot не импортируют.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.fsm.storage.base import BaseStorage, StorageKey

from app.core.logging import get_logger

logger = get_logger(__name__)

PROMPT_ID_KEY = "_prompt_message_id"
PROMPT_AT_KEY = "_prompt_asked_at"
PROMPT_PLACEHOLDER_KEY = "_prompt_placeholder"
PROMPT_KEYS = (PROMPT_ID_KEY, PROMPT_AT_KEY, PROMPT_PLACEHOLDER_KEY)

# Решение владельца 03.10: брошенный ввод держит отсрочку не дольше 5 минут.
INPUT_DEFER = timedelta(minutes=5)
# Подсказка в поле ввода — лимит Telegram 64 символа.
PLACEHOLDER_LIMIT = 64


class InputGate:
    def __init__(self, storage: BaseStorage | None, bot_id: int | None) -> None:
        self._storage = storage
        self._bot_id = bot_id

    async def entering(self, telegram_id: int, now: datetime | None = None) -> bool:
        """Пользователь отвечает на числовой вопрос бота (личный чат: chat_id
        = user_id = telegram_id)."""
        if self._storage is None or self._bot_id is None:
            return False
        key = StorageKey(bot_id=self._bot_id, chat_id=telegram_id, user_id=telegram_id)
        try:
            data: dict[str, Any] = await self._storage.get_data(key)
        except Exception:
            logger.warning("Состояние ввода не прочитано — уведомление не откладываю")
            return False
        asked = data.get(PROMPT_AT_KEY)
        if not data.get(PROMPT_ID_KEY) or not asked:
            return False
        try:
            asked_at = datetime.fromisoformat(asked)
        except (TypeError, ValueError):
            return False
        return (now or datetime.now(UTC)) - asked_at <= INPUT_DEFER
