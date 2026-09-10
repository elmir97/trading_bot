"""Тест прерывания FSM-формы командой.

Регрессия из реального использования: пользователь начал добавлять сделку,
затем ввёл /stats — команда попала в форму как название инструмента, и бот
предложил выбрать направление для «инструмента» /STATS.

Проверяем через настоящий Dispatcher aiogram: важен именно порядок роутеров
и фактическое разрешение состояния, а не имитация.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User as TgUser

from app.bot.handlers.fsm_guard import command_interrupts_form

CHAT_ID = 555
USER_ID = 777
BOT_ID = 999


class DemoForm(StatesGroup):
    symbol = State()


@pytest_asyncio.fixture
async def bot(monkeypatch):  # type: ignore[no-untyped-def]
    """Bot с фиктивным токеном и заглушкой вместо сетевых вызовов.

    Диспетчеру нужен bot.id (число перед двоеточием в токене), а guard
    отправляет пользователю сообщение об отмене формы — этот вызов и
    перехватываем, иначе тест уходил бы в сеть.
    """
    sent: list[object] = []

    async def fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        sent.append(method)
        return None

    monkeypatch.setattr(Bot, "__call__", fake_call)

    instance = Bot(token=f"{BOT_ID}:AAFakeTokenForDispatcherTests")
    instance.sent_methods = sent  # type: ignore[attr-defined]
    yield instance
    await instance.session.close()


@pytest.fixture
def dispatcher() -> tuple[Dispatcher, list[str]]:
    """Диспетчер с тем же порядком роутеров, что и в main.py.

    Guard регистрируется в свежем Router, а не берётся модульный: один
    и тот же экземпляр Router нельзя подключить к двум диспетчерам, и
    переиспользование ломало бы соседние тесты.
    """
    calls: list[str] = []

    guard = Router(name="guard")
    guard.message.register(
        command_interrupts_form, StateFilter("*"), F.text.startswith("/")
    )

    form = Router(name="form")

    @form.message(DemoForm.symbol)
    async def catch_symbol(message: Message) -> None:
        calls.append(f"form:{message.text}")

    commands = Router(name="commands")

    @commands.message(Command("stats"))
    async def stats(message: Message) -> None:
        calls.append("command:/stats")

    @commands.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        # Как настоящий cmd_start: сбрасывает состояние сам.
        await state.clear()
        calls.append("command:/start")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(guard)
    # Как в main.py: common (тут — commands, с /start) регистрируется
    # раньше trades (тут — form) — иначе catch-all форма перехватит
    # /start раньше, чем до него дойдёт настоящий обработчик команды.
    dp.include_router(commands)
    dp.include_router(form)
    return dp, calls


def make_message(text: str, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=TgUser(id=USER_ID, is_bot=False, first_name="Tester"),
        text=text,
    )


async def _context(dp: Dispatcher, state: State | None) -> FSMContext:
    ctx = FSMContext(
        storage=dp.storage,
        key=StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID),
    )
    await ctx.set_state(state)
    return ctx


async def test_command_does_not_leak_into_form(dispatcher, bot) -> None:  # type: ignore[no-untyped-def]
    """Главная регрессия: /stats не должен стать названием инструмента."""
    dp, calls = dispatcher
    ctx = await _context(dp, DemoForm.symbol)

    await dp.feed_update(
        bot=bot, update=Update(update_id=1, message=make_message("/stats"))
    )

    assert "form:/stats" not in calls
    # Форма отменена, повторная отправка команды сработает штатно.
    assert await ctx.get_state() is None


async def test_command_works_after_form_cancelled(dispatcher, bot) -> None:  # type: ignore[no-untyped-def]
    dp, calls = dispatcher
    await _context(dp, DemoForm.symbol)

    await dp.feed_update(
        bot=bot, update=Update(update_id=1, message=make_message("/stats"))
    )
    await dp.feed_update(
        bot=bot, update=Update(update_id=2, message=make_message("/stats", 2))
    )

    assert calls == ["command:/stats"]


async def test_plain_text_still_reaches_form(dispatcher, bot) -> None:  # type: ignore[no-untyped-def]
    """Обычный ввод не затрагивается: форма работает как прежде."""
    dp, calls = dispatcher
    ctx = await _context(dp, DemoForm.symbol)

    await dp.feed_update(
        bot=bot, update=Update(update_id=3, message=make_message("BTC-USDT"))
    )

    assert calls == ["form:BTC-USDT"]
    assert await ctx.get_state() == DemoForm.symbol.state


async def test_command_without_active_form_passes_through(dispatcher, bot) -> None:  # type: ignore[no-untyped-def]
    """Без формы guard не вмешивается — команда идёт напрямую."""
    dp, calls = dispatcher
    await _context(dp, None)

    await dp.feed_update(
        bot=bot, update=Update(update_id=4, message=make_message("/stats"))
    )

    assert calls == ["command:/stats"]


async def test_start_resets_form_in_one_message(dispatcher, bot) -> None:  # type: ignore[no-untyped-def]
    """/start — особый случай: сам сбрасывает форму, guard не должен его
    перехватывать и заставлять слать команду дважды (см. cmd_start)."""
    dp, calls = dispatcher
    ctx = await _context(dp, DemoForm.symbol)

    await dp.feed_update(
        bot=bot, update=Update(update_id=5, message=make_message("/start"))
    )

    assert calls == ["command:/start"]
    assert "form:/start" not in calls
    assert await ctx.get_state() is None
