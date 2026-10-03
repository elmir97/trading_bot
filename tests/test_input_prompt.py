"""Числовой ввод (03.10): вопрос с ForceReply, PromptMiddleware, InputGate и
отсрочка некритичных уведомлений, пока пользователь вводит число.

Жалоба владельца 03.10: фоновые уведомления уводили вопрос бота вверх, и
непонятно, куда отвечать."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ForceReply, Message

from app.bot.prompts import ASK_TEXT, REPEAT_TEXT, PromptMiddleware, ask_number
from app.core.input_prompt import (
    INPUT_DEFER,
    PROMPT_AT_KEY,
    PROMPT_ID_KEY,
    InputGate,
)
from app.trading.enums import TradeSide
from tests import test_reconciler_worker as harness

# Фикстура харнесса reconciler (настоящий BingXClient на живых ответах демо, БД).
ctx = harness.ctx

D = Decimal
BOT_ID, TG = 42, 777


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.deleted: list[int] = []
        self._next = 100

    async def send_message(self, chat_id: int, text: str, reply_markup=None, **_: Any):  # type: ignore[no-untyped-def]
        self._next += 1
        self.sent.append((text, reply_markup))
        return SimpleNamespace(message_id=self._next)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)


def _message(bot: FakeBot) -> MagicMock:
    message = MagicMock(spec=Message)
    message.bot = bot
    message.chat = SimpleNamespace(id=TG)
    message.answer = AsyncMock()
    return message


def _state(storage: MemoryStorage) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=BOT_ID, chat_id=TG, user_id=TG))


# --- ask_number ----------------------------------------------------------------


async def test_ask_number_force_reply_with_placeholder() -> None:
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await ask_number(_message(bot), state, "Цена стопа, например 1.4750 " + "x" * 80)
    [(text, markup)] = bot.sent
    assert text == ASK_TEXT
    assert isinstance(markup, ForceReply) and markup.force_reply
    # 03.10, проверка на телефоне: selective в личном чате не нацелен ни на
    # кого — клиент не открывал «ответ на» и не показывал подсказку.
    assert not markup.selective
    assert markup.input_field_placeholder.startswith("Цена стопа, например 1.4750")
    assert len(markup.input_field_placeholder) == 64
    data = await state.get_data()
    assert data[PROMPT_ID_KEY] == 101 and data[PROMPT_AT_KEY]


async def test_new_question_deletes_previous() -> None:
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await ask_number(_message(bot), state, "Цена входа числом")
    await ask_number(_message(bot), state, "Цена стопа числом")
    assert bot.deleted == [101]
    assert (await state.get_data())[PROMPT_ID_KEY] == 102


# --- PromptMiddleware ----------------------------------------------------------


async def _through_middleware(state: FSMContext, event: Any, handler) -> None:  # type: ignore[no-untyped-def]
    await PromptMiddleware()(handler, event, {"state": state})


async def test_step_changed_question_removed() -> None:
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await state.set_state("form:entry")
    await ask_number(_message(bot), state, "Цена входа числом")

    async def accept(event, data):  # type: ignore[no-untyped-def]
        await state.set_state("form:strategy")   # следующий шаг — кнопки

    await _through_middleware(state, _message(bot), accept)
    assert bot.deleted == [101]
    assert PROMPT_ID_KEY not in await state.get_data()


async def test_cleared_state_question_removed() -> None:
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await state.set_state("form:entry")
    await ask_number(_message(bot), state, "Цена входа числом")

    async def cancel(event, data):  # type: ignore[no-untyped-def]
        await state.clear()                       # команда посреди формы / «Отмена»

    callback = MagicMock(spec=CallbackQuery)
    callback.message = _message(bot)
    await _through_middleware(state, callback, cancel)
    assert bot.deleted == [101]


async def test_rejected_answer_asks_again() -> None:
    """Ответ не принят — то же состояние: вопрос задаётся заново (после
    ответа клиент снимает «ответ на»)."""
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await state.set_state("form:entry")
    await ask_number(_message(bot), state, "Цена входа числом")

    async def reject(event, data):  # type: ignore[no-untyped-def]
        await event.answer("Не понял, введи число")

    await _through_middleware(state, _message(bot), reject)
    assert bot.deleted == [101]
    text, markup = bot.sent[-1]
    assert text == REPEAT_TEXT and markup.input_field_placeholder == "Цена входа числом"


async def test_next_numeric_step_keeps_single_question() -> None:
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await state.set_state("form:entry")
    await ask_number(_message(bot), state, "Цена входа числом")

    async def next_numeric(event, data):  # type: ignore[no-untyped-def]
        await state.set_state("form:stop")
        await ask_number(event, state, "Цена стопа числом")

    await _through_middleware(state, _message(bot), next_numeric)
    assert bot.deleted == [101] and len(bot.sent) == 2


# --- InputGate -----------------------------------------------------------------


async def test_gate_entering_within_five_minutes() -> None:
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    gate = InputGate(storage, BOT_ID)
    assert not await gate.entering(TG)                     # вопроса нет
    await ask_number(_message(bot), state, "Цена стопа числом")
    assert await gate.entering(TG)
    later = datetime.now(UTC) + INPUT_DEFER + timedelta(seconds=5)
    assert not await gate.entering(TG, later)              # брошенный ввод
    await state.clear()
    assert not await gate.entering(TG)


async def test_gate_without_storage_never_defers() -> None:
    assert not await InputGate(None, BOT_ID).entering(TG)


# --- отсрочка: дневные рассылки -----------------------------------------------


class _Gate:
    def __init__(self, value: bool) -> None:
        self.value = value

    async def entering(self, telegram_id: int, now: datetime | None = None) -> bool:
        return self.value


async def test_daily_report_deferred_loss_limit_not(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.core.config import Settings
    from app.workers import daily as daily_module
    from app.workers.notifier import Delivery

    sent: list[str] = []

    async def send(bot, telegram_id, text, **_):  # type: ignore[no-untyped-def]
        sent.append(text)
        return Delivery.DELIVERED

    monkeypatch.setattr(daily_module, "send_notification", send)
    jobs = daily_module.DailyJobs(
        MagicMock(), MagicMock(), Settings(), input_gate=_Gate(True),  # type: ignore[call-arg]
    )
    user = SimpleNamespace(id=1, telegram_id=TG)
    assert await jobs._deliver(user, "daily_report", "итоги", date(2026, 10, 3)) is False
    assert await jobs._deliver(user, "execution_digest", "сводка", date(2026, 10, 3)) is False
    assert sent == []
    assert await jobs._deliver(user, "daily_limit_reached", "лимит", date(2026, 10, 3)) is True
    assert sent == ["лимит"]


# --- отсрочка: reconciler (ORPHAN «не в журнале») ------------------------------


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")
async def test_orphan_deferred_while_entering_then_sent(ctx) -> None:  # type: ignore[no-untyped-def]
    from app.core.security import SecretCipher
    from app.trading.enums import ReconciliationKind
    from app.workers.reconciler import Reconciler
    from tests.test_reconciler_worker import FakeBot as ReconBot
    from tests.test_reconciler_worker import _events

    settings, db, session, user, demo = ctx
    bot, gate = ReconBot(), _Gate(True)
    cipher = SecretCipher(settings.encryption_key.get_secret_value())
    reconciler = Reconciler(bot, db, settings, cipher, None, input_gate=gate)

    await reconciler.run()                     # живая позиция LINK без сделки — ORPHAN
    [orphan] = [e for e in await _events(db, user.id)
                if e.kind is ReconciliationKind.ORPHAN_POSITION]
    assert orphan.notified_at is None and orphan.attempts == 0 and bot.sent == []

    gate.value = False
    await reconciler.run()
    [orphan] = [e for e in await _events(db, user.id)
                if e.kind is ReconciliationKind.ORPHAN_POSITION]
    assert orphan.notified_at is not None and orphan.attempts == 1
    assert len(bot.sent) == 1


# --- этап 4: вопрос о цене -------------------------------------------------------


def test_example_level_rounds_to_tick() -> None:
    from app.bot.handlers.position_actions import example_level

    assert example_level(D("1.5054"), TradeSide.LONG, True, 4) == D("1.4753")    # стоп ниже
    assert example_level(D("1.5054"), TradeSide.LONG, False, 4) == D("1.5355")   # тейк выше
    assert example_level(D("1.5054"), TradeSide.SHORT, True, 4) == D("1.5355")   # стоп выше
    assert example_level(D("100"), TradeSide.SHORT, False, 2) == D("98.00")


async def test_stage4_price_prompt_force_reply_and_cancel(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.bot.handlers import position_actions as handlers

    async def placeholder(*a, **k):  # type: ignore[no-untyped-def]
        return "Цена стопа, например 1.4753"

    async def record(db, **kw):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(handlers, "_placeholder", placeholder)
    monkeypatch.setattr(handlers, "record_callback", record)
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "pa:sl:XRP-USDT:L"
    callback.id = "cb"
    callback.message = _message(bot)
    callback.message.message_id = 55
    callback.message.photo = None
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    user = SimpleNamespace(id=7, telegram_id=TG)

    await handlers.open_action(callback, state, MagicMock(), user, None, None, MagicMock(), None)  # type: ignore[arg-type]
    edited = callback.message.edit_text.await_args.args[0]
    assert "Жду цену стопа" in edited and "ответь числом" in edited
    keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in keyboard.inline_keyboard for b in row][0] == "pc:x"
    [(_, markup)] = bot.sent
    assert isinstance(markup, ForceReply)
    assert markup.input_field_placeholder == "Цена стопа, например 1.4753"
    assert await state.get_state() == handlers.PositionActionStates.price.state

    await handlers.cancel_input(callback, state)
    assert await state.get_state() is None


# --- настоящие обработчики через PromptMiddleware (03.10) -------------------------


def _user_message(bot: FakeBot, text: str) -> MagicMock:
    message = _message(bot)
    message.text = text
    return message


async def test_stage4_invalid_price_keeps_input_and_asks_again(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """«Абв» на вопрос о цене стопа: состояние ввода остаётся, вопрос — заново;
    число — карточка, вопрос удалён. До правки «Абв» снимало ввод и
    становилось карточкой-отказом «⛔ Цена стопа — положительное число»."""
    from app.bot.handlers import position_actions as handlers

    shown: list[dict[str, object]] = []

    async def show_card(target, service, kind, params, symbol, side, *, edit):  # type: ignore[no-untyped-def]
        shown.append(params)

    monkeypatch.setattr(handlers, "_show_card", show_card)
    monkeypatch.setattr(handlers, "_service", lambda *a, **k: None)
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await state.set_state(handlers.PositionActionStates.price)
    await state.update_data(kind="MOVE_STOP", symbol="XRP-USDT", side="SHORT")
    await ask_number(_message(bot), state, "Цена стопа, например 1.5371")

    async def run(text: str) -> MagicMock:
        message = _user_message(bot, text)

        async def handler(event, data):  # type: ignore[no-untyped-def]
            await handlers.price_entered(event, state, None, None, None, None, None)  # type: ignore[arg-type]

        await PromptMiddleware()(handler, message, {"state": state})
        return message

    bad = await run("Абв")
    assert "нужна цена числом" in bad.answer.await_args.args[0]
    assert await state.get_state() == handlers.PositionActionStates.price.state
    assert shown == []
    text, markup = bot.sent[-1]
    assert text == REPEAT_TEXT and markup.input_field_placeholder == "Цена стопа, например 1.5371"

    await run("1,4850")
    assert shown == [{"level": "1.4850"}]
    assert await state.get_state() is None
    assert (await state.get_data()) == {}
    assert PROMPT_ID_KEY not in await state.get_data()


async def test_wizard_entry_price_invalid_asks_again() -> None:
    """Мастер «Добавить сделку», шаг «Цена входа»: «abc» — состояние остаётся,
    вопрос заново; число — следующий шаг (стоп) со своим вопросом."""
    from app.bot.handlers import trades as handlers
    from app.bot.states.trade import AddTradeStates

    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await state.set_state(AddTradeStates.entry_price)
    await state.update_data(symbol="XRP-USDT", side="LONG")
    await ask_number(_message(bot), state, "Цена входа числом")

    async def run(text: str) -> MagicMock:
        message = _user_message(bot, text)

        async def handler(event, data):  # type: ignore[no-untyped-def]
            await handlers.set_entry_price(event, state)

        await PromptMiddleware()(handler, message, {"state": state})
        return message

    await run("abc")
    assert await state.get_state() == AddTradeStates.entry_price.state
    assert bot.sent[-1][0] == REPEAT_TEXT
    await run("1.5054")
    assert await state.get_state() == AddTradeStates.stop_loss.state
    assert bot.sent[-1][1].input_field_placeholder.startswith("Цена стопа")


async def test_question_after_cleared_state_removes_previous_once() -> None:
    """03.10.2026: хендлер сбросил состояние и задал новый вопрос в том же
    апдейте (старт /risk посреди мастера) — старый вопрос не висит в чате и
    не удаляется дважды."""
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await state.set_state("form:entry")
    await ask_number(_message(bot), state, "Цена входа числом")

    async def restart(event, data):  # type: ignore[no-untyped-def]
        await state.clear()
        await state.set_state("calc:balance")
        await ask_number(event, state, "Баланс в USDT, например 1000")

    await _through_middleware(state, _message(bot), restart)
    assert bot.deleted == [101]
    assert (await state.get_data())[PROMPT_ID_KEY] == 102


async def test_rejected_input_and_deletion_logged(caplog) -> None:  # type: ignore[no-untyped-def]
    bot, storage = FakeBot(), MemoryStorage()
    state = _state(storage)
    await state.set_state("form:entry")
    await ask_number(_message(bot), state, "Цена входа числом")

    async def reject(event, data):  # type: ignore[no-untyped-def]
        await event.answer("Не понял, введи число")

    message = _message(bot)
    message.text = "84 590 — вход по рынку"
    with caplog.at_level("INFO", logger="app.bot.prompts"):
        await _through_middleware(state, message, reject)
    rejected = [r for r in caplog.records if r.getMessage() == "Ввод не принят"]
    assert len(rejected) == 1
    assert rejected[0].text == "84 590 — вход по" and rejected[0].state == "form:entry"
    deleted = [r for r in caplog.records if r.getMessage() == "Вопрос с ForceReply удалён"]
    assert [r.message_id for r in deleted] == [101]
