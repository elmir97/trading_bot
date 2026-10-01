"""Middleware доступа и ошибок пишут telegram_id только маской."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram.types import Chat, Message
from aiogram.types import User as TgUser

from app.bot.middlewares.access import AccessMiddleware
from app.bot.middlewares.errors import ErrorMiddleware

TG_ID = 1234567890


def _all_logged(caplog) -> str:  # type: ignore[no-untyped-def]
    return " ".join(
        r.getMessage() + " " + " ".join(map(str, vars(r).values())) for r in caplog.records
    )


def _message() -> Message:
    return Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=TG_ID, type="private"), text="x"
    )


async def test_access_denied_log_is_masked(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def no_reply(event) -> None:  # type: ignore[no-untyped-def]
        return None

    mw = AccessMiddleware(frozenset({1}))
    monkeypatch.setattr(AccessMiddleware, "_reject", staticmethod(no_reply))
    with caplog.at_level(logging.WARNING):
        await mw(
            lambda e, d: None,
            _message(),
            {"event_from_user": TgUser(id=TG_ID, is_bot=False, first_name="x")},
        )
    denied = [r for r in caplog.records if r.getMessage() == "Отклонён доступ"]
    assert denied and denied[0].tg == "12…890"  # type: ignore[attr-defined]
    assert str(TG_ID) not in _all_logged(caplog)


async def test_unhandled_error_log_is_masked(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def boom(event, data):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    async def no_notify(event) -> None:  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ErrorMiddleware, "_notify", staticmethod(no_notify))
    with caplog.at_level(logging.ERROR):
        await ErrorMiddleware()(
            boom, _message(), {"event_from_user": TgUser(id=TG_ID, is_bot=False, first_name="x")}
        )
    errors = [r for r in caplog.records if r.getMessage() == "Необработанная ошибка"]
    assert errors and errors[0].tg == "12…890"  # type: ignore[attr-defined]
    assert str(TG_ID) not in _all_logged(caplog)
