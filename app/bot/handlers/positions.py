"""Экран «💼 Позиции» (этап 3): позиции с биржи и связь с журналом.

Позиции — с биржи, счёт из настроек (user.settings.active_exchange_mode),
как у остальных экранов биржи: get_positions + get_open_orders без символа —
два запроса. Стоп и тейк — из openOrders (app/execution/position_view.py).
Позиция без сделки в журнале — кнопка «📥 В журнал»: в журнал заносится только
ТЕКУЩАЯ позиция — исполнения её входа за IMPORT_DAYS дней
(HistoryImporter.import_open_position); история целиком — /import. Ниже —
открытые сделки только из журнала (без позиции на бирже): их закрывают как
раньше, вводом цены выхода (app/bot/handlers/trades.py).

Сам экран биржу только читает. На позицию — одна кнопка «⚙️ XRP LONG»: экран
действий этой позиции (стоп в безубыток, стоп, тейк, 25%/50%, закрыть всё),
кнопки ведут в карточки подтверждения (app/bot/handlers/position_actions.py,
этап 4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.exchange import ExchangeCB, _describe, _market_cache
from app.bot.handlers.position_actions import SIDE_BY_CODE, SIDE_CODE, action_buttons
from app.bot.keyboards.main import MenuCallback
from app.bot.keyboards.trade import TradeCB
from app.bot.messaging import edit_or_replace
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.trade import Trade
from app.database.models.user import User
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import ExchangeAuthError, ExchangeClient, ExchangeError, Position
from app.execution.position_view import (
    PositionView,
    build_views,
    journal_only,
    render_journal_trade,
    render_position,
)
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.services.import_service import HistoryImporter
from app.trading.enums import TradeSide
from app.trading.risk import tz_offset_for

router = Router(name="positions")
logger = get_logger(__name__)

IMPORT_DAYS = 30


class PositionsCB:
    REFRESH = "pos:refresh"
    IMPORT = "pos:imp:"    # + SYMBOL:L|S
    ACTIONS = "pos:act:"   # + SYMBOL:L|S


def _short(symbol: str) -> str:
    return symbol.replace("-USDT", "")


def _position_ref(symbol: str, side: TradeSide) -> str:
    return f"{symbol}:{SIDE_CODE[side]}"


def _parse_ref(data: str, prefix: str) -> tuple[str, TradeSide] | None:
    symbol, _, code = data.removeprefix(prefix).rpartition(":")
    if not symbol or code not in SIDE_BY_CODE:
        return None
    return symbol, SIDE_BY_CODE[code]


def positions_keyboard(views: list[PositionView], journal: list[Trade]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for view in views:
        # Одна строка на позицию: действия — на своём экране (подписи
        # полностью, по две в ряд), здесь только вход в него и «В журнал».
        p = view.position
        ref = _position_ref(p.symbol, p.side)
        row = [InlineKeyboardButton(
            text=f"⚙️ {_short(p.symbol)} {p.side.value}",
            callback_data=f"{PositionsCB.ACTIONS}{ref}",
        )]
        if view.trade is None:
            row.append(InlineKeyboardButton(
                text=f"📥 В журнал: {_short(p.symbol)} {p.side.value}",
                callback_data=f"{PositionsCB.IMPORT}{ref}",
            ))
        builder.row(*row)
    for trade in journal:
        builder.row(InlineKeyboardButton(
            text=f"{trade.symbol} {trade.side.value} #{trade.id}",
            callback_data=f"{TradeCB.CLOSE}{trade.id}",
        ))
    builder.row(
        InlineKeyboardButton(text="🔄 Обновить", callback_data=PositionsCB.REFRESH),
        InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN),
    )
    return builder.as_markup()


def actions_keyboard(symbol: str, side: TradeSide) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    buttons = [
        InlineKeyboardButton(text=text, callback_data=data)
        for text, data in action_buttons(symbol, side)
    ]
    for i in range(0, len(buttons), 2):
        builder.row(*buttons[i:i + 2])
    builder.row(InlineKeyboardButton(text="◀️ К позициям", callback_data=PositionsCB.REFRESH))
    return builder.as_markup()


def render_actions(view: PositionView, precision: int | None) -> str:
    p = view.position
    return "\n".join([
        f"<b>⚙️ Действия · {p.symbol} {p.side.value}</b>",
        "",
        render_position(view, precision),
        "",
        "<i>Каждое действие — карточка с расчётом и «Да»/«Нет», без «Да» на биржу "
        "ничего не уходит.</i>",
    ])


def render_screen(
    *,
    mode_label: str,
    views: list[PositionView] | None,
    exchange_note: str | None,
    journal: list[Trade],
    precision: dict[str, int],
    note: str | None = None,
) -> str:
    lines = [f"<b>💼 Позиции · {mode_label}</b>"]
    if note:
        lines += ["", note]
    lines.append("")
    if exchange_note is not None:
        lines.append(exchange_note)
    elif not views:
        lines.append("На бирже открытых позиций нет.")
    else:
        for view in views:
            lines += [render_position(view, precision.get(view.position.symbol)), ""]
        if lines[-1] == "":
            lines.pop()
    if journal:
        lines += ["", "<b>Только в журнале</b>"]
        lines += [render_journal_trade(t) for t in journal]
        lines += ["", "<i>Сделку журнала закрывают вводом цены выхода — кнопка ниже.</i>"]
    return "\n".join(lines)


async def _entry_balance(
    client: ExchangeClient, position: Position, user_id: int
) -> Decimal | None:
    """Баланс на входе для «📥 В журнал» (03.10.2026, блокер live Б1): equity
    счёта минус нереализованный PnL этой позиции — баланс, каким он был без
    неё. Без баланса сделка не входит в процент лимитов убытка (способ
    «от баланса на входе каждой сделки»). Не получен — None и WARNING;
    сделку всё равно заносим, с пометкой в ответе."""
    try:
        equity = (await client.get_balance()).equity
    except ExchangeError:
        logger.warning(
            "В журнал: баланс счёта не получен — сделка без баланса на входе",
            extra={"user_id": user_id, "symbol": position.symbol},
        )
        return None
    return equity - position.unrealized_pnl


async def _load(
    session: AsyncSession, user: User, settings: Settings, cipher: SecretCipher
) -> tuple[list[PositionView] | None, str | None, list[Trade], dict[str, int]]:
    """(позиции, причина, почему их нет, сделки только из журнала, точность цен)."""
    open_trades = await TradeRepository(session).list_open(user.id)
    try:
        client = await ExchangeFactory(settings, cipher).for_user(
            session, user.id, mode=user.settings.active_exchange_mode
        )
    except ExchangeAuthError:
        return None, "🔑 Ключи биржи не подключены — показан только журнал.", open_trades, {}
    try:
        positions = await client.get_positions()
        orders = await client.get_open_orders() if positions else []
        precision: dict[str, int] = {}
        if positions:
            infos = await MarketDataService(client, _market_cache).get_symbols()
            precision = {i.symbol: i.price_precision for i in infos}
    except ExchangeError as exc:
        logger.warning("Позиции с биржи не получены", extra={"user_id": user.id})
        return None, _describe(exc), open_trades, {}
    finally:
        await client.close()
    views = build_views(
        positions, orders, open_trades, account_mode=user.settings.active_exchange_mode
    )
    return views, None, journal_only(open_trades, views), precision


async def _show(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
    note: str | None = None,
) -> None:
    views, exchange_note, journal, precision = await _load(session, user, settings, cipher)
    if exchange_note is not None:
        # Без биржи все открытые сделки — «только журнал».
        journal = await TradeRepository(session).list_open(user.id)
    text = render_screen(
        mode_label=user.settings.active_exchange_mode.label,
        views=views,
        exchange_note=exchange_note,
        journal=journal,
        precision=precision,
        note=note,
    )
    keyboard = positions_keyboard(views or [], journal)
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await edit_or_replace(event.message, text, keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


@router.callback_query(
    F.data.in_({MenuCallback.OPEN_POSITIONS, ExchangeCB.POSITIONS, PositionsCB.REFRESH})
)
@router.message(Command("open"))
async def show_positions(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    await _show(event, session, user, settings, cipher)


@router.callback_query(F.data.startswith(PositionsCB.ACTIONS))
async def show_actions(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    """Экран действий одной позиции — с биржи заново: позиция могла закрыться."""
    ref = _parse_ref(str(callback.data), PositionsCB.ACTIONS)
    if ref is None:
        await callback.answer("Кнопка устарела — открой «Позиции» заново.", show_alert=True)
        return
    symbol, side = ref
    views, exchange_note, _, precision = await _load(session, user, settings, cipher)
    view = next(
        (v for v in views or [] if v.position.symbol == symbol and v.position.side == side),
        None,
    )
    if view is None:
        note = exchange_note or f"Позиции {symbol} {side.value} на бирже уже нет."
        await _show(callback, session, user, settings, cipher, note=note)
        return
    if isinstance(callback.message, Message):
        await edit_or_replace(
            callback.message, render_actions(view, precision.get(symbol)),
            actions_keyboard(symbol, side),
        )
    await callback.answer()


@router.callback_query(F.data.startswith(PositionsCB.IMPORT))
async def import_position(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    """В журнал — только текущая открытая позиция (исполнения её входа за
    IMPORT_DAYS дней), без закрытых сделок символа: история — /import.
    Сделку создаёт пользователь кнопкой; reconciler сам их не создаёт
    (позиция без сделки — только уведомление)."""
    ref = _parse_ref(str(callback.data), PositionsCB.IMPORT)
    if ref is None:
        await callback.answer("Кнопка устарела — открой «Позиции» заново.", show_alert=True)
        return
    symbol, side = ref
    label = f"{symbol} {side.value}"
    await callback.answer("Заношу в журнал…")
    try:
        client = await ExchangeFactory(settings, cipher).for_user(
            session, user.id, mode=user.settings.active_exchange_mode
        )
    except ExchangeAuthError as exc:
        await _show(callback, session, user, settings, cipher, note=_describe(exc))
        return
    end = datetime.now(UTC)
    try:
        position = next(
            (p for p in await client.get_positions() if p.symbol == symbol and p.side == side),
            None,
        )
        if position is None:
            note = f"📥 {label}: позиции на бирже уже нет — в журнал ничего не занесено."
        else:
            balance = await _entry_balance(client, position, user.id)
            outcome = await HistoryImporter(
                client, TradeRepository(session), user.id,
                account_mode=user.settings.active_exchange_mode,
                journal_cutoff=user.settings.journal_cutoff_at,
                tz_offset=tz_offset_for(user.settings.timezone),
            ).import_open_position(
                end - timedelta(days=IMPORT_DAYS), end,
                symbol=symbol, side=side, quantity=position.quantity,
                position_id=position.position_id, account_balance=balance,
            )
            if outcome.trade is not None:
                note = f"📥 {label}: в журнале — сделка #{outcome.trade.id}."
                if balance is None:
                    note += (
                        " ⚠️ Баланс счёта не получен — сделка не войдёт в процент "
                        "лимитов убытка."
                    )
            else:
                note = f"📥 {label}: {outcome.refusal} Историю целиком — /import."
    except ExchangeError as exc:
        logger.warning("Позиция в журнал не занесена", extra={"user_id": user.id})
        note = _describe(exc)
    finally:
        await client.close()
    await _show(callback, session, user, settings, cipher, note=note)
