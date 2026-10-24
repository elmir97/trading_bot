"""Кнопки сообщения сверки «позиция на бирже, сделки в журнале нет»
(ORPHAN_POSITION, очередь п.3, 24.10): «📥 В журнал» прямо в сообщении и
«💼 Позиции». После занесения — только «💼 Позиции»."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards.main import MenuCallback
from app.trading.enums import TradeSide

IMPORT_ORPHAN = "pos:impn:"   # + SYMBOL:L|S (не путать с pos:imp: экрана «Позиции»)
ORPHAN_REFUSED = "📥 Не занесено: "   # строка отказа под текстом сообщения сверки
SIDE_CODE = {TradeSide.LONG: "L", TradeSide.SHORT: "S"}   # как у pos:act: и pos:imp:


def orphan_import_data(symbol: str, side: TradeSide) -> str:
    return f"{IMPORT_ORPHAN}{symbol}:{SIDE_CODE[side]}"


def _positions_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="💼 Позиции", callback_data=MenuCallback.OPEN_POSITIONS)


def orphan_keyboard(symbol: str, side: TradeSide) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 В журнал", callback_data=orphan_import_data(symbol, side)),
        _positions_button(),
    ]])


def orphan_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_positions_button()]])
