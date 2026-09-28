"""Тесты send_notification_photo: фото с подписью, откат на текст при сбое.

Требование: генерация/отправка графика не должна ронять уведомление —
если send_photo падает по любой причине, текст всё равно должен уйти.
"""

from __future__ import annotations

from aiogram.exceptions import TelegramForbiddenError

from app.workers.notifier import send_notification, send_notification_photo


class FakeBot:
    def __init__(self, *, photo_fails: bool = False) -> None:
        self.photo_fails = photo_fails
        self.sent_photos: list[tuple[int, str]] = []
        self.sent_messages: list[tuple[int, str]] = []
        self.sent_markups: list[object] = []

    async def send_photo(self, chat_id: int, photo, caption: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        if self.photo_fails:
            raise RuntimeError("telegram недоступен")
        self.sent_photos.append((chat_id, caption))
        self.sent_markups.append(reply_markup)

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent_messages.append((chat_id, text))
        self.sent_markups.append(reply_markup)


class ForbiddenBot(FakeBot):
    async def send_photo(self, chat_id: int, photo, caption: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        raise TelegramForbiddenError(method=None, message="forbidden")  # type: ignore[arg-type]


async def test_photo_sent_when_ok() -> None:
    bot = FakeBot()
    await send_notification_photo(bot, 1, b"fake-png-bytes", "текст сигнала")

    assert bot.sent_photos == [(1, "текст сигнала")]
    assert bot.sent_messages == []


async def test_falls_back_to_text_when_photo_send_fails() -> None:
    bot = FakeBot(photo_fails=True)
    await send_notification_photo(bot, 1, b"fake-png-bytes", "текст сигнала")

    assert bot.sent_photos == []
    assert bot.sent_messages == [(1, "текст сигнала")]  # текст всё равно ушёл


async def test_forbidden_does_not_fall_back_to_text() -> None:
    """Бот заблокирован — это не сбой доставки картинки, а устойчивое
    состояние получателя: ретраить текстом смысла нет."""
    bot = ForbiddenBot()
    await send_notification_photo(bot, 1, b"fake-png-bytes", "текст сигнала")

    assert bot.sent_photos == []
    assert bot.sent_messages == []


async def test_reply_markup_forwarded_to_send_message() -> None:
    bot = FakeBot()
    markup = object()
    await send_notification(bot, 1, "текст", reply_markup=markup)

    assert bot.sent_messages == [(1, "текст")]
    assert bot.sent_markups == [markup]


async def test_reply_markup_omitted_keeps_two_arg_call() -> None:
    """Без reply_markup вызов остаётся двухаргументным — существующие
    дублёры бота (без keyword-параметра) не должны ломаться."""
    bot = FakeBot()
    await send_notification(bot, 1, "текст")

    assert bot.sent_messages == [(1, "текст")]
    assert bot.sent_markups == [None]


async def test_reply_markup_forwarded_to_send_photo() -> None:
    bot = FakeBot()
    markup = object()
    await send_notification_photo(bot, 1, b"fake-png-bytes", "подпись", reply_markup=markup)

    assert bot.sent_photos == [(1, "подпись")]
    assert bot.sent_markups == [markup]


async def test_reply_markup_forwarded_on_photo_fallback_to_text() -> None:
    """Раздел 5 ТЗ: если фото не ушло, кнопка не должна потеряться —
    текстовый фолбэк обязан унести тот же reply_markup."""
    bot = FakeBot(photo_fails=True)
    markup = object()
    await send_notification_photo(bot, 1, b"fake-png-bytes", "подпись", reply_markup=markup)

    assert bot.sent_photos == []
    assert bot.sent_messages == [(1, "подпись")]
    assert bot.sent_markups == [markup]


# --- 28.09: исход отправки — Delivery, не bool ---------------------------------
# Delivery берётся из модуля внутри теста: на старом коде его нет, и падать
# должны только эти тесты, а не весь файл на импорте.


class NetworkFailBot(FakeBot):
    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        from aiogram.exceptions import TelegramNetworkError

        raise TelegramNetworkError(method=None, message="Request timeout error")  # type: ignore[arg-type]


class ForbiddenTextBot(FakeBot):
    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        raise TelegramForbiddenError(method=None, message="forbidden")  # type: ignore[arg-type]


async def test_delivered_on_success() -> None:
    from app.workers import notifier

    assert await send_notification(FakeBot(), 1, "текст") is notifier.Delivery.DELIVERED


async def test_network_error_is_failed_not_final() -> None:
    from app.workers import notifier

    delivery = await send_notification(NetworkFailBot(), 1, "текст")
    assert delivery is notifier.Delivery.FAILED
    assert delivery.final is False


async def test_forbidden_is_final_and_warns(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    from app.workers import notifier

    with caplog.at_level(logging.INFO, logger="app.workers.notifier"):
        delivery = await send_notification(ForbiddenTextBot(), 1, "текст")

    assert delivery is notifier.Delivery.FORBIDDEN
    assert delivery.final is True
    [record] = [r for r in caplog.records if "бот заблокирован" in r.getMessage()]
    assert record.levelno == logging.WARNING


async def test_photo_forbidden_is_forbidden() -> None:
    from app.workers import notifier

    assert (
        await send_notification_photo(ForbiddenBot(), 1, b"png", "подпись")
        is notifier.Delivery.FORBIDDEN
    )


def test_delivery_refuses_bool() -> None:
    """StrEnum всегда истинен — «if await send_notification(...)» молча
    считал бы доставленным и сбой. Приведение к bool обязано падать."""
    import pytest

    from app.workers import notifier

    with pytest.raises(TypeError):
        bool(notifier.Delivery.FAILED)
