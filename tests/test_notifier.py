"""Тесты send_notification_photo: фото с подписью, откат на текст при сбое.

Требование: генерация/отправка графика не должна ронять уведомление —
если send_photo падает по любой причине, текст всё равно должен уйти.
"""

from __future__ import annotations

from aiogram.exceptions import TelegramForbiddenError

from app.workers.notifier import send_notification_photo


class FakeBot:
    def __init__(self, *, photo_fails: bool = False) -> None:
        self.photo_fails = photo_fails
        self.sent_photos: list[tuple[int, str]] = []
        self.sent_messages: list[tuple[int, str]] = []

    async def send_photo(self, chat_id: int, photo, caption: str) -> None:
        if self.photo_fails:
            raise RuntimeError("telegram недоступен")
        self.sent_photos.append((chat_id, caption))

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent_messages.append((chat_id, text))


class ForbiddenBot(FakeBot):
    async def send_photo(self, chat_id: int, photo, caption: str) -> None:
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
