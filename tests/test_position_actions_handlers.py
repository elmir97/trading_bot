"""Кнопки действий с позицией (этап 4, app/bot/handlers/position_actions.py):
callback_data в лимите Telegram, кнопка роста риска, журнал нажатий первым
действием, сбой записи блокирует «Да». Сервис подменён — без биржи и БД."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.handlers import position_actions as handlers
from app.bot.handlers.position_actions import (
    AUDIT_FAILED_TEXT,
    ActionCB,
    action_buttons,
    card_keyboard,
)
from app.execution.position_action_service import ConfirmOutcome
from app.trading.enums import ExecutionCallbackAction, TradeSide
from app.workers.execution_digest import build_stats, detect_anomalies, render_execution_digest


def test_action_buttons_fit_telegram_limit() -> None:
    for side in TradeSide:
        for text, data in action_buttons("1000000MOG-USDT", side):
            assert data.startswith(ActionCB.OPEN)
            assert len(data.encode()) <= 64, text


def test_card_keyboard_risk_increase_has_no_plain_yes() -> None:
    plain = [b.callback_data for row in card_keyboard(7, False).inline_keyboard for b in row]
    risky = [b.callback_data for row in card_keyboard(7, True).inline_keyboard for b in row]
    assert plain == ["pm:y:7", "pm:n:7"]
    assert risky == ["pm:r:7", "pm:n:7"]


def _callback(data: str) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.id = "cb-1"
    message = MagicMock(spec=Message)
    message.message_id = 55
    message.chat = SimpleNamespace(id=1)
    message.photo = None
    message.edit_text = AsyncMock()
    callback.message = message
    callback.answer = AsyncMock()
    return callback


class _Service:
    def __init__(self) -> None:
        self.confirmed: list[tuple] = []

    async def confirm(self, action_id, *, message_id, risk_confirmed):  # type: ignore[no-untyped-def]
        self.confirmed.append((action_id, message_id, risk_confirmed))
        return ConfirmOutcome("✅ готово")

    async def decline(self, action_id):  # type: ignore[no-untyped-def]
        return "Отменено — на биржу ничего не отправлено."


@pytest.fixture
def env(monkeypatch):  # type: ignore[no-untyped-def]
    state = SimpleNamespace(service=_Service(), audits=[], audit_fails=False)

    async def record(db, **kw):  # type: ignore[no-untyped-def]
        if state.audit_fails:
            raise RuntimeError("db down")
        state.audits.append((kw["action"], kw["position_action_id"]))

    monkeypatch.setattr(handlers, "record_callback", record)
    monkeypatch.setattr(handlers, "_service", lambda *a, **k: state.service)
    return state


async def _decide(data: str):  # type: ignore[no-untyped-def]
    callback = _callback(data)
    user = SimpleNamespace(id=7, telegram_id=123456789)
    await handlers.decide(callback, MagicMock(), user, None, None, MagicMock(), None)  # type: ignore[arg-type]
    return callback


async def test_yes_audited_then_confirmed(env) -> None:  # type: ignore[no-untyped-def]
    callback = await _decide("pm:y:11")
    assert env.audits == [(ExecutionCallbackAction.PM_YES, 11)]
    assert env.service.confirmed == [(11, 55, False)]
    callback.message.edit_text.assert_awaited_once()


async def test_yes_risk_passes_confirmation(env) -> None:  # type: ignore[no-untyped-def]
    await _decide("pm:r:11")
    assert env.audits == [(ExecutionCallbackAction.PM_YES_RISK, 11)]
    assert env.service.confirmed == [(11, 55, True)]


async def test_audit_failure_blocks_yes(env) -> None:  # type: ignore[no-untyped-def]
    env.audit_fails = True
    callback = await _decide("pm:y:11")
    assert env.service.confirmed == []
    callback.answer.assert_awaited_once_with(AUDIT_FAILED_TEXT, show_alert=True)


async def test_no_declines_without_confirm(env) -> None:  # type: ignore[no-untyped-def]
    callback = await _decide("pm:n:11")
    assert env.audits == [(ExecutionCallbackAction.PM_NO, 11)]
    assert env.service.confirmed == []
    assert "ничего не отправлено" in callback.message.edit_text.await_args.args[0]


async def test_lock_busy_is_alert_and_card_kept(env) -> None:  # type: ignore[no-untyped-def]
    async def busy(*a, **k):  # type: ignore[no-untyped-def]
        return ConfirmOutcome("Действие с этой позицией уже выполняется.", final=False)

    env.service.confirm = busy
    callback = await _decide("pm:y:11")
    callback.answer.assert_awaited_once_with("Действие с этой позицией уже выполняется.",
                                             show_alert=True)
    callback.message.edit_text.assert_not_awaited()


async def test_bad_id_is_stale(env) -> None:  # type: ignore[no-untyped-def]
    callback = await _decide("pm:y:abc")
    callback.answer.assert_awaited_once_with("Кнопка устарела.", show_alert=True)
    assert env.audits == []


def test_digest_counts_actions_and_flags_failed() -> None:
    from app.database.models.position_action import PositionAction
    from app.trading.enums import PositionActionKind, PositionActionStatus

    def a(status: PositionActionStatus) -> PositionAction:
        return PositionAction(user_id=1, symbol="XRP-USDT", side=TradeSide.LONG,
                              kind=PositionActionKind.CLOSE_FULL, status=status)

    stats = build_stats([], [a(PositionActionStatus.DONE), a(PositionActionStatus.DRY_RUN),
                             a(PositionActionStatus.FAILED)])
    text = render_execution_digest(stats)
    assert "Действия с позициями: карточек 3 (выполнено 1, сухой прогон 1, сбой 1)" in text
    assert "действия с позициями не выполнены — 1" in detect_anomalies(stats)
