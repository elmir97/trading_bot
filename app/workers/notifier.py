"""Единая точка отправки фоновых уведомлений.

Каждый воркер сначала проверяет notification_enabled, потом зовёт
send_notification — так проверка настройки не размазана по трём файлам, а
блокировка бота одним пользователем не мешает разослать уведомления
остальным (та же изоляция, что и в требовании к самим циклам, только на
уровне одного получателя).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message

from app.core.logging import get_logger
from app.database.models.reconciliation_event import ReconciliationEvent
from app.database.models.user import DEFAULT_NOTIFICATIONS, UserSettings

logger = get_logger(__name__)

NOTIFICATION_LABELS: dict[str, str] = {
    "sl_approaching": "🛑 приближение к стопу",
    "tp_approaching": "🎯 приближение к тейку",
    "daily_report": "📄 дневная сводка",
    "daily_limit_reached": "🛑 дневной лимит убытка",
    "execution_digest": "📊 сводка исполнения",
}


class Delivery(StrEnum):
    """Исход отправки (28.09). Три исхода, не bool: «сбой сети» повторяют,
    «бот заблокирован» — окончательный исход, повторять незачем.

    Приведение к bool запрещено: StrEnum всегда истинен, и старая проверка
    «if await send_notification(...)» молча считала бы доставленным любой
    исход — пусть лучше падает."""

    DELIVERED = "DELIVERED"
    FAILED = "FAILED"  # сбой сети / API Telegram — повторить позже
    FORBIDDEN = "FORBIDDEN"  # пользователь заблокировал бота — не повторять

    @property
    def final(self) -> bool:
        """Повторять больше не нужно: доставлено или запрещено."""
        return self is not Delivery.FAILED

    def __bool__(self) -> bool:
        raise TypeError("Delivery не приводится к bool — сравнивай с Delivery.*")


def notification_enabled(settings: UserSettings | None, kind: str) -> bool:
    """Отсутствие настройки (в т.ч. у старых пользователей без нового ключа
    в JSONB) трактуется как "включено" — новые типы уведомлений по
    умолчанию не должны требовать миграции данных, только кода."""
    default = DEFAULT_NOTIFICATIONS.get(kind, True)
    if settings is None:
        return default
    return bool(settings.notifications.get(kind, default))


def approach_enabled(settings: UserSettings | None, kind: str) -> bool:
    """Этап 5: kind — "sl_approaching" / "tp_approaching". Без нового ключа
    в JSONB — старый общий tp_sl_approaching (до этапа 5 один переключатель
    на оба), без него — включено. Данные в M2 не переносятся."""
    if settings is not None and kind in settings.notifications:
        return bool(settings.notifications[kind])
    if settings is not None and "tp_sl_approaching" in settings.notifications:
        return bool(settings.notifications["tp_sl_approaching"])
    return DEFAULT_NOTIFICATIONS.get(kind, True)


async def send_notification(
    bot: Bot,
    telegram_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Delivery:
    """Исход отправки (см. send_notification_message)."""
    delivery, _ = await send_notification_message(
        bot, telegram_id, text, reply_markup=reply_markup
    )
    return delivery


async def send_notification_message(
    bot: Bot,
    telegram_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> tuple[Delivery, Message | None]:
    """Исход отправки и отправленное сообщение (09.10: его message_id пишет
    журнал исходящих app/bot/outbox.py; дублёр бота в тестах может вернуть
    не Message — тогда None). Сбой не пробрасывается (изоляция получателей, см.
    docstring модуля), но вызывающий код знает о нём: сканеру это нужно,
    чтобы не записать снимок неотправленного уведомления (шаг 15.5.2а),
    остальным — чтобы ставить отметку доставки только после успеха и
    повторять только FAILED (28.09)."""
    try:
        # reply_markup передаётся только когда задан: тестовые дублёры бота
        # (FakeBot) в существующих тестах принимают send_message(chat_id, text)
        # без лишних именованных аргументов.
        if reply_markup is not None:
            sent = await bot.send_message(telegram_id, text, reply_markup=reply_markup)
        else:
            sent = await bot.send_message(telegram_id, text)
    except TelegramForbiddenError:
        # Пользователь заблокировал бота или удалил чат — это не сбой
        # доставки, который стоит ретраить, а устойчивое состояние.
        logger.warning(
            "Уведомление не доставлено: бот заблокирован",
            extra={"telegram_id": telegram_id},
        )
        return Delivery.FORBIDDEN, None
    except TelegramAPIError:
        logger.exception(
            "Не удалось отправить уведомление", extra={"telegram_id": telegram_id}
        )
        return Delivery.FAILED, None
    return Delivery.DELIVERED, sent if isinstance(sent, Message) else None


async def deliver_event(
    bot: Bot, event: ReconciliationEvent, telegram_id: int, now: datetime, text: str
) -> Delivery:
    """Одна попытка доставки события reconciliation_events (28.09): и для
    reconciler, и для тревог read-back на пути «Да». notified_at — только
    после успеха; бот заблокирован — gave_up_at, без повторов; сбой сети —
    событие остаётся на переотправку (app/execution/redelivery.py)."""
    event.attempts = (event.attempts or 0) + 1
    event.last_attempt_at = now
    delivery = await send_notification(bot, telegram_id, text)
    if delivery is Delivery.DELIVERED:
        event.notified_at = datetime.now(UTC)
    elif delivery is Delivery.FORBIDDEN:
        event.gave_up_at = now
    return delivery


async def send_notification_photo(
    bot: Bot,
    telegram_id: int,
    photo: bytes,
    caption: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Delivery:
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
        logger.warning(
            "Уведомление не доставлено: бот заблокирован",
            extra={"telegram_id": telegram_id},
        )
        return Delivery.FORBIDDEN
    except Exception:
        logger.exception(
            "Не удалось отправить график, шлём текстом",
            extra={"telegram_id": telegram_id},
        )
        return await send_notification(bot, telegram_id, caption, reply_markup=reply_markup)
    return Delivery.DELIVERED
