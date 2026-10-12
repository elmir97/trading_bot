"""Итог открытия в чате — общий модуль app/bot/opening_result.py (Mini App,
подэтап 0): воркер и API не тянут хендлеры, отправка итога одна."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from app.bot import opening_result
from app.bot.handlers.open_trade import OpenCB
from app.bot.handlers.positions import PositionsCB
from app.database.models.outgoing_message import OutgoingMeta
from app.trading.enums import OpeningStatus, TradeSide


def test_callback_data_single_source() -> None:
    assert OpenCB.CLOSE == opening_result.CLOSE == "ot:x:"
    assert OpenCB.CANCEL_LIMIT == opening_result.CANCEL_LIMIT == "ot:c:"
    assert OpenCB.RECALC == opening_result.RECALC == "ot:recalc"
    assert PositionsCB.ACTIONS == opening_result.POSITION_ACTIONS == "pos:act:"
    kb = opening_result.position_keyboard("XRP-USDT", TradeSide.SHORT)
    button = kb.inline_keyboard[0][0]
    assert (button.text, button.callback_data) == ("⚙️ Позиция XRP SHORT", "pos:act:XRP-USDT:S")


def test_worker_does_not_import_handlers() -> None:
    """Пакет app.workers не импортирует app.bot.handlers (докстринг пакета);
    итог открытия — через opening_result."""
    code = (
        "import sys, app.workers.openings, app.bot.opening_result\n"
        "bad = sorted(m for m in sys.modules if m.startswith('app.bot.handlers'))\n"
        "print(bad); sys.exit(1 if bad else 0)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, stdin=subprocess.DEVNULL,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


class _Sent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []


async def test_send_opening_result_routes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sent = _Sent()

    async def notify(bot, telegram_id, text, *, reply_markup=None):  # type: ignore[no-untyped-def]
        sent.calls.append(("notify", reply_markup))

    async def outbox_send(bot, db, meta, telegram_id, text, markup):  # type: ignore[no-untyped-def]
        sent.calls.append(("outbox", (meta.kind, markup)))

    monkeypatch.setattr(opening_result, "send_notification", notify)
    monkeypatch.setattr(opening_result.outbox, "send", outbox_send)
    position = ("XRP-USDT", TradeSide.LONG)

    await opening_result.send_opening_result(
        object(), object(), 1, "⏳", status=OpeningStatus.WORKING, opening_id=None,  # type: ignore[arg-type]
        position=None,
    )
    meta = OutgoingMeta(user_id=1, kind="OPEN_DONE", trade_opening_id=7, trade_id=31)
    await opening_result.send_opening_result(
        object(), object(), 1, "✅", status=OpeningStatus.DONE, opening_id=7,  # type: ignore[arg-type]
        position=position, meta=meta,
    )
    # без meta — уведомлением; WORKING без opening_id — без кнопки отмены
    assert sent.calls[0] == ("notify", None)
    kind, markup = sent.calls[1][1]
    assert sent.calls[1][0] == "outbox" and kind == "OPEN_DONE"
    assert markup == opening_result.position_keyboard(*position)


async def test_worker_notify_passes_opening_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Цикл открытий: итог WORKING с meta — кнопка «Отменить лимит» этого открытия."""
    from app.workers.openings import OpeningsWorker

    markups: list[Any] = []

    async def outbox_send(bot, db, meta, telegram_id, text, markup):  # type: ignore[no-untyped-def]
        markups.append(markup)

    monkeypatch.setattr(opening_result.outbox, "send", outbox_send)
    worker = OpeningsWorker(object(), object(), object(), object(), None, factory=object())  # type: ignore[arg-type]
    meta = OutgoingMeta(user_id=1, kind="OPEN_WORKING", trade_opening_id=42)
    await worker.notify(1, "⏳", None, meta=meta, status=OpeningStatus.WORKING)
    assert markups[0].inline_keyboard[0][0].callback_data == f"{opening_result.CANCEL_LIMIT}42"
