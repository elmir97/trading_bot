"""Единая точка отправки фоновых уведомлений.

Каждый воркер сначала проверяет notification_enabled, потом зовёт
send_notification — так проверка настройки не размазана по трём файлам, а
блокировка бота одним пользователем не мешает разослать уведомления
остальным (та же изоляция, что и в требовании к самим циклам, только на
уровне одного получателя).
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from app.core.logging import get_logger
from app.database.models.user import DEFAULT_NOTIFICATIONS, UserSettings

logger = get_logger(__name__)

NOTIFICATION_LABELS: dict[str, str] = {
    "setup_ready": "🎯 сетапы READY",
    "setup_forming": "🌱 формирующиеся сетапы",
    "setup_charts": "🖼️ графики к сигналам",
    "tp_sl_approaching": "📍 приближение к TP/SL",
    "daily_report": "📄 дневная сводка",
    "daily_limit_reached": "🛑 дневной лимит убытка",
    "execution_digest": "📊 сводка исполнения",
}


def notification_enabled(settings: UserSettings | None, kind: str) -> bool:
    """Отсутствие настройки (в т.ч. у старых пользователей без нового ключа
    в JSONB) трактуется как "включено" — новые типы уведомлений по
    умолчанию не должны требовать миграции данных, только кода."""
    default = DEFAULT_NOTIFICATIONS.get(kind, True)
    if settings is None:
        return default
    return bool(settings.notifications.get(kind, default))


async def send_notification(
    bot: Bot,
    telegram_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        # reply_markup передаётся только когда задан: тестовые дублёры бота
        # (FakeBot) в существующих тестах принимают send_message(chat_id, text)
        # без лишних именованных аргументов.
        if reply_markup is not None:
            await bot.send_message(telegram_id, text, reply_markup=reply_markup)
        else:
            await bot.send_message(telegram_id, text)
    except TelegramForbiddenError:
        # Пользователь заблокировал бота или удалил чат — это не сбой
        # доставки, который стоит ретраить, а устойчивое состояние.
        logger.info(
            "Уведомление не доставлено: бот заблокирован",
            extra={"telegram_id": telegram_id},
        )
    except TelegramAPIError:
        logger.exception(
            "Не удалось отправить уведомление", extra={"telegram_id": telegram_id}
        )


async def send_notification_photo(
    bot: Bot,
    telegram_id: int,
    photo: bytes,
    caption: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Фото с подписью; при любой проблеме с фото (не только с сетью —
    сюда же попадает, например, подпись длиннее 1024 символов) откатывается
    на обычный текст, чтобы уведомление не терялось только из-за картинки."""
    try:
        if reply_markup is not None:
            await bot.send_photo(
                telegram_id,
                BufferedInputFile(photo, filename="setup.png"),
                caption=caption,
                reply_markup=reply_markup,
            )
        else:
            await bot.send_photo(
                telegram_id,
                BufferedInputFile(photo, filename="setup.png"),
                caption=caption,
            )
    except TelegramForbiddenError:
        logger.info(
            "Уведомление не доставлено: бот заблокирован",
            extra={"telegram_id": telegram_id},
        )
    except Exception:
        logger.exception(
            "Не удалось отправить график, шлём текстом",
            extra={"telegram_id": telegram_id},
        )
        await send_notification(bot, telegram_id, caption, reply_markup=reply_markup)
