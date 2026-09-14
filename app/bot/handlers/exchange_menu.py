"""Раздел «Биржа» — точка входа ко всему, что связано с BingX."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.exchange import ExchangeCB
from app.bot.keyboards.main import MenuCallback
from app.core.config import Settings
from app.core.security import SecretCipher
from app.database.models.user import User
from app.services.exchange_factory import ExchangeFactory

router = Router(name="exchange_menu")


def exchange_menu(has_keys: bool) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    # Цены доступны без ключей — рыночные данные публичны.
    builder.row(
        InlineKeyboardButton(text="💲 Цены", callback_data=ExchangeCB.PRICES),
        InlineKeyboardButton(
            text="🔍 Сверить инструменты", callback_data=ExchangeCB.CHECK_SYMBOLS
        ),
    )
    if has_keys:
        builder.row(
            InlineKeyboardButton(text="💰 Баланс", callback_data=ExchangeCB.BALANCE),
            InlineKeyboardButton(
                text="📊 Позиции", callback_data=ExchangeCB.POSITIONS
            ),
            InlineKeyboardButton(text="📋 Ордера", callback_data=ExchangeCB.ORDERS),
        )
        builder.row(
            InlineKeyboardButton(
                text="⬇️ Импорт истории", callback_data=ExchangeCB.IMPORT
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text="🔑 Подключить ключи", callback_data="set:api"
            )
        )
    builder.row(
        InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN)
    )
    return builder


@router.callback_query(F.data == MenuCallback.EXCHANGE)
async def show_exchange_menu(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    active_mode = user.settings.active_exchange_mode
    has_keys = await ExchangeFactory(settings, cipher).has_credentials(
        session, user.id, mode=active_mode
    )

    if has_keys:
        text = (
            f"<b>BingX · {active_mode.label}</b>\n\nКлючи подключены.\n\n"
            "Баланс и позиции берутся с биржи напрямую. "
            "Импорт добавляет закрытые сделки в журнал.\n\n"
            "Счёт переключается в «Настройках»."
        )
    else:
        text = (
            f"<b>BingX · {active_mode.label}</b>\n\nКлючи не подключены.\n\n"
            "Цены доступны без них. Для баланса, позиций и импорта "
            "истории нужен API-ключ, привязанный к IP этого сервера. "
            "Подключи его в «Настройках»."
        )

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text, reply_markup=exchange_menu(has_keys).as_markup()
        )
    await callback.answer()
