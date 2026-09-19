"""Работа с биржей из бота: баланс, позиции, импорт истории.

Все обращения к бирже обёрнуты в единый обработчик ошибок: пользователь
должен получить понятную причину («ключи не привязаны к IP»), а не
техническое исключение и не молчание.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, TypeVar

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.formatting import fmt_amount, fmt_money, fmt_num, fmt_price, fmt_qty
from app.bot.keyboards.main import MenuCallback, back_to, nav_row
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.user import User
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeRateLimitError,
    ExchangeUnavailableError,
)
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.services.import_service import HistoryImporter, default_import_range

router = Router(name="exchange")
logger = get_logger(__name__)

# Кэш общий на процесс: рыночные данные одинаковы для всех пользователей,
# а лимиты биржи считаются по IP сервера, а не по аккаунту.
_market_cache = TTLCache()

T = TypeVar("T")


class ExchangeCB:
    BALANCE = "ex:balance"
    POSITIONS = "ex:positions"
    ORDERS = "ex:orders"
    IMPORT = "ex:import"
    IMPORT_CONFIRM = "ex:import_go:"
    PRICES = "ex:prices"
    CHECK_SYMBOLS = "ex:check_symbols"
    FIX_SYMBOLS = "ex:fix_symbols"


def _describe(error: Exception) -> str:
    """Переводит сбой биржи в объяснение, с которым можно что-то сделать."""
    if isinstance(error, ExchangeAuthError):
        return f"🔑 {error}"
    if isinstance(error, ExchangeRateLimitError):
        return (
            "⏳ Превышен лимит запросов к бирже. "
            "Подожди минуту и попробуй снова."
        )
    if isinstance(error, ExchangeUnavailableError):
        return (
            "📡 Биржа не отвечает. Возможны технические работы "
            "или проблемы со связью — попробуй позже."
        )
    if isinstance(error, ExchangeError):
        return f"⚠️ Биржа вернула ошибку: {error}"
    return "⚠️ Не удалось получить данные с биржи. Подробности в логе."


async def _reply(event: Message | CallbackQuery, text: str, keyboard=None) -> None:  # type: ignore[no-untyped-def]
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


async def _with_exchange(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
    action: Callable[[Any], Awaitable[str]],
) -> None:
    """Создаёт клиент, выполняет действие, гарантированно закрывает сессию.

    Этап 15.4в: счёт, для которого берутся ключи и хост, — тот, что выбран
    в настройках (user.settings.active_exchange_mode), а не тот, куда
    реально уходят ордера. Здесь только чтение (баланс, позиции, импорт)."""
    factory = ExchangeFactory(settings, cipher)
    try:
        client = await factory.for_user(session, user.id, mode=user.settings.active_exchange_mode)
    except ExchangeAuthError as exc:
        await _reply(event, _describe(exc), back_to(MenuCallback.EXCHANGE))
        return

    try:
        text = await action(client)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка обращения к бирже", extra={"user_id": user.id})
        text = _describe(exc)
    finally:
        await client.close()

    await _reply(event, text, back_to(MenuCallback.EXCHANGE))


# ---------------------------------------------------------------------------
# Баланс
# ---------------------------------------------------------------------------


@router.message(Command("balance"))
@router.callback_query(F.data == ExchangeCB.BALANCE)
async def show_balance(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    async def action(client) -> str:  # type: ignore[no-untyped-def]
        balance = await client.get_balance()
        free_ratio = (
            balance.available / balance.equity * 100
            if balance.equity > 0
            else Decimal(0)
        )
        return (
            f"<b>Баланс BingX · {user.settings.active_exchange_mode.label}</b>\n\n"
            f"Эквити: {fmt_amount(balance.equity)} {balance.asset}\n"
            f"Свободно: {fmt_amount(balance.available)} "
            f"({fmt_num(free_ratio.quantize(Decimal('0.1')))}%)\n"
            f"В позициях: {fmt_amount(balance.used_margin)}\n"
            f"Нереализованный PnL: {fmt_money(balance.unrealized_pnl)}"
        )

    await _with_exchange(event, session, user, settings, cipher, action)


# ---------------------------------------------------------------------------
# Позиции на бирже
# ---------------------------------------------------------------------------


@router.callback_query(F.data == ExchangeCB.POSITIONS)
async def show_exchange_positions(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    async def action(client) -> str:  # type: ignore[no-untyped-def]
        positions = await client.get_positions()
        if not positions:
            return "На бирже нет открытых позиций."

        lines = [f"<b>Позиции на бирже · {user.settings.active_exchange_mode.label}</b>", ""]
        for position in positions:
            icon = "🟢" if position.side.value == "LONG" else "🔴"
            lines.append(
                f"{icon} <b>{position.symbol}</b> {position.side.value}\n"
                f"Объём: {fmt_qty(position.quantity)} · "
                f"вход {fmt_num(position.entry_price)}\n"
                f"Текущая: {fmt_num(position.mark_price)} · "
                f"плечо {position.leverage}x\n"
                f"PnL: {fmt_money(position.unrealized_pnl)} USDT"
            )
            if position.liquidation_price:
                lines.append(
                    f"⚠️ Ликвидация: {fmt_num(position.liquidation_price)}"
                )
            lines.append("")

        lines.append(
            "<i>Это позиции с биржи. Журнал ведётся отдельно — "
            "импортируй историю, чтобы они попали в статистику.</i>"
        )
        return "\n".join(lines)

    await _with_exchange(callback, session, user, settings, cipher, action)


# ---------------------------------------------------------------------------
# Открытые (выставленные) ордера на бирже
# ---------------------------------------------------------------------------


def _is_own_order(client_order_id: str) -> bool:
    """Ордер отправлен ботом по сигналу — см. app/execution/models.py
    client_order_id(): f"tj{signal_id}{user_id}{role}". Конвенция бота,
    биржевой клиент про неё ничего не знает — поэтому проверка тут,
    не в OpenOrder."""
    return client_order_id.startswith("tj")


def _render_open_order(order, precision) -> str:  # type: ignore[no-untyped-def]
    price_precision = precision.price_precision if precision else None
    qty_precision = precision.quantity_precision if precision else 8

    icon = "🟢" if order.position_side == "LONG" else "🔴"
    lines = [
        f"{icon} <b>{order.symbol}</b> {order.side} {order.order_type} · {order.status}",
        f"Объём: {fmt_qty(order.quantity, qty_precision)} по "
        f"{fmt_price(order.price, price_precision)} · плечо {order.leverage}x",
    ]
    if order.take_profit is not None:
        lines.append(f"Тейк: {fmt_price(order.take_profit.trigger_price, price_precision)}")
    if order.stop_loss is not None:
        lines.append(f"Стоп: {fmt_price(order.stop_loss.trigger_price, price_precision)}")
    return "\n".join(lines)


@router.callback_query(F.data == ExchangeCB.ORDERS)
async def show_exchange_orders(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    async def action(client) -> str:  # type: ignore[no-untyped-def]
        orders = await client.get_open_orders()
        if not orders:
            return "На бирже нет выставленных ордеров."

        precision_by_symbol = {info.symbol: info for info in await client.get_symbols()}

        own = [o for o in orders if _is_own_order(o.client_order_id)]
        manual = [o for o in orders if not _is_own_order(o.client_order_id)]

        lines = [f"<b>Ордера на бирже · {user.settings.active_exchange_mode.label}</b>"]

        if own:
            lines.append("")
            lines.append("<b>Свои (по сигналам)</b>")
            for order in own:
                lines.append("")
                lines.append(
                    _render_open_order(order, precision_by_symbol.get(order.symbol))
                )

        if manual:
            lines.append("")
            lines.append("<b>Выставленные вручную</b>")
            for order in manual:
                lines.append("")
                lines.append(
                    _render_open_order(order, precision_by_symbol.get(order.symbol))
                )

        return "\n".join(lines)

    await _with_exchange(callback, session, user, settings, cipher, action)


# ---------------------------------------------------------------------------
# Цены
# ---------------------------------------------------------------------------


@router.callback_query(F.data == ExchangeCB.PRICES)
@router.message(Command("prices"))
async def show_prices(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    """Цены инструментов из торгового плана.

    Ключи не нужны: рыночные данные публичны, и раздел работает даже
    без подключённой биржи.
    """
    from app.database.repositories.user import UserRepository

    plan = await UserRepository(session).get_trading_plan(user.id)
    symbols = (plan.allowed_symbols if plan else []) or ["BTC-USDT", "ETH-USDT"]

    client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
    market = MarketDataService(client, _market_cache)

    try:
        prices = await market.get_prices(symbols[:10])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось получить цены")
        await _reply(event, _describe(exc), back_to(MenuCallback.EXCHANGE))
        return
    finally:
        await client.close()

    lines = ["<b>Текущие цены</b>", ""]
    for symbol, price in prices.items():
        name = symbol.replace("-USDT", "")
        lines.append(
            f"{name}: {fmt_num(price)}" if price is not None
            else f"{name}: нет данных"
        )

    await _reply(event, "\n".join(lines), back_to(MenuCallback.EXCHANGE))


# ---------------------------------------------------------------------------
# Импорт истории
# ---------------------------------------------------------------------------


def _import_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for days, label in ((7, "Неделя"), (30, "Месяц"), (90, "3 месяца"), (365, "Год")):
        builder.button(text=label, callback_data=f"{ExchangeCB.IMPORT_CONFIRM}{days}")
    builder.adjust(2)
    builder.row(*nav_row(MenuCallback.EXCHANGE))
    return builder


@router.callback_query(F.data == ExchangeCB.IMPORT)
@router.message(Command("import"))
async def ask_import_period(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    # Проверяем ключи до выбора периода: предлагать выбор, зная, что
    # он упрётся в ошибку, — значит тратить время пользователя впустую.
    if not await ExchangeFactory(settings, cipher).has_credentials(
        session, user.id, mode=user.settings.active_exchange_mode
    ):
        await _reply(
            event,
            "🔑 Импорт требует ключей BingX.\n\n"
            "Подключи их в «Настройках» — нужен ключ только на чтение, "
            "привязанный к IP сервера.",
            back_to(MenuCallback.EXCHANGE),
        )
        return

    await _reply(
        event,
        "<b>Импорт истории с BingX</b>\n\n"
        "Сделки будут добавлены в журнал с точными ценами и комиссиями "
        "биржи. Стратегию и причину входа биржа не знает — их нужно "
        "будет разметить самому.\n\n"
        "Повторный импорт того же периода дублей не создаёт.\n\n"
        "За какой период загрузить?",
        _import_keyboard().as_markup(),
    )


@router.callback_query(F.data.startswith(ExchangeCB.IMPORT_CONFIRM))
async def run_import(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    days = int(str(callback.data).removeprefix(ExchangeCB.IMPORT_CONFIRM))

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"⏳ Загружаю историю за {days} дн. Это может занять "
            f"несколько минут — биржа отдаёт данные небольшими окнами."
        )
    await callback.answer()

    back = back_to(ExchangeCB.IMPORT, with_menu=True)

    factory = ExchangeFactory(settings, cipher)
    try:
        client = await factory.for_user(session, user.id, mode=user.settings.active_exchange_mode)
    except ExchangeAuthError as exc:
        await _reply(callback, _describe(exc), back)
        return

    try:
        # Баланс нужен, чтобы PnL импортированных сделок считался в
        # процентах от депозита. Без него сделка сохранится, но
        # процент останется пустым.
        balance = None
        try:
            balance = (await client.get_balance()).equity
        except ExchangeError:
            logger.warning("Баланс недоступен, импорт без процентов")

        start, end = default_import_range(days)
        importer = HistoryImporter(client, TradeRepository(session), user.id)
        result = await importer.import_period(start, end, account_balance=balance)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Импорт не удался", extra={"user_id": user.id})
        await _reply(callback, _describe(exc), back)
        return
    finally:
        await client.close()

    text = f"<b>Импорт за {days} дн.</b>\n\n{result.render()}"
    if result.trades_created:
        text += (
            "\n\n📌 Импортированные сделки не размечены. "
            "Пока у них не указана стратегия, они не попадают в срезы "
            "по стратегиям и ошибкам."
        )

    await _reply(callback, text, back)


# ---------------------------------------------------------------------------
# Сверка инструментов с биржей
# ---------------------------------------------------------------------------


def _base_asset(symbol: str) -> str:
    """Базовый актив из тикера: BTC-USDT → BTC."""
    return symbol.split("-")[0].upper()


# Переименования, которые не поймать сравнением префиксов: новое имя не
# является ни продолжением старого, ни его сокращением.
#
# Реальный тикер Toncoin в API BingX (/openApi/swap/v2/quote/contracts) —
# GRAMTON-USDT, поле asset = "GRAMTON". «GRAM-USDT» — это только
# displayName для интерфейса биржи, самого символа с таким именем в
# списке контрактов нет; сверяться нужно с тем, что отдаёт API, а не с
# тем, что показывает сайт. Строки «TON» и «GRAMTON» не пересекаются по
# началу, так что эвристика по общему префиксу ниже этот случай не находит.
BASE_ASSET_ALIASES: dict[str, str] = {
    "TON": "GRAMTON",
}


def _match_symbols(
    planned: list[str], available: list[str]
) -> tuple[list[str], dict[str, str], list[str]]:
    """Сверяет инструменты плана с фактическим списком биржи.

    Возвращает совпавшие, исправимые (по базовому активу) и потерянные.

    Смысл в том, что тикеры у бирж расходятся: Toncoin на BingX — это
    GRAMTON-USDT, а не TON-USDT (и не GRAM-USDT — так биржа только
    отображает пару в интерфейсе). Неверное имя не роняет бота,
    инструмент просто молча исчезает из цен и анализа — заметить это
    трудно.
    """
    available_set = set(available)
    by_base: dict[str, str] = {}
    for symbol in available:
        by_base.setdefault(_base_asset(symbol), symbol)

    matched: list[str] = []
    fixable: dict[str, str] = {}
    missing: list[str] = []

    for symbol in planned:
        if symbol in available_set:
            matched.append(symbol)
            continue

        base = _base_asset(symbol)
        candidate = by_base.get(base)

        if candidate is None:
            candidate = by_base.get(BASE_ASSET_ALIASES.get(base, ""))

        if candidate is None:
            # Биржи сокращают и удлиняют тикеры: тикер у BingX может быть
            # длиннее или короче привычного. Ищем по началу названия, но
            # только если кандидат единственный: «ETH» подошло бы и к
            # ETHFI, а молча подменить инструмент хуже, чем не подменить.
            similar = sorted(
                {
                    exchange_base
                    for exchange_base in by_base
                    if exchange_base.startswith(base) or base.startswith(exchange_base)
                }
            )
            if len(similar) == 1:
                candidate = by_base[similar[0]]

        if candidate:
            fixable[symbol] = candidate
        else:
            missing.append(symbol)

    return matched, fixable, missing


@router.callback_query(F.data == ExchangeCB.CHECK_SYMBOLS)
async def check_symbols(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    from app.database.repositories.user import UserRepository

    plan = await UserRepository(session).get_trading_plan(user.id)
    if plan is None or not plan.allowed_symbols:
        await _reply(callback, "В торговом плане нет инструментов.", back_to(MenuCallback.EXCHANGE))
        return

    client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
    market = MarketDataService(client, _market_cache)
    try:
        available = [info.symbol for info in await market.get_symbols()]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось получить список инструментов")
        await _reply(callback, _describe(exc), back_to(MenuCallback.EXCHANGE))
        return
    finally:
        await client.close()

    matched, fixable, missing = _match_symbols(plan.allowed_symbols, available)

    lines = ["<b>Сверка инструментов с BingX</b>", ""]
    lines.append(f"✅ Совпадают: {len(matched)}")

    if fixable:
        lines.append("")
        lines.append("<b>Названы иначе на бирже:</b>")
        lines += [f"• {old} → {new}" for old, new in fixable.items()]
    if missing:
        lines.append("")
        lines.append("<b>Нет на бирже:</b>")
        lines += [f"• {symbol}" for symbol in missing]

    builder = InlineKeyboardBuilder()
    if fixable:
        lines.append("")
        lines.append(
            "<i>Пока названия не совпадают, эти инструменты не попадают "
            "ни в цены, ни в анализ.</i>"
        )
        builder.row(
            InlineKeyboardButton(
                text="✏️ Исправить названия", callback_data=ExchangeCB.FIX_SYMBOLS
            )
        )
    elif not missing:
        lines.append("")
        lines.append("Все инструменты плана есть на бирже.")

    builder.row(*nav_row(MenuCallback.EXCHANGE))
    await _reply(callback, "\n".join(lines), builder.as_markup())


@router.callback_query(F.data == ExchangeCB.FIX_SYMBOLS)
async def fix_symbols(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    from app.database.repositories.user import UserRepository

    back = back_to(ExchangeCB.CHECK_SYMBOLS, with_menu=True)

    plan = await UserRepository(session).get_trading_plan(user.id)
    if plan is None:
        await _reply(callback, "Торговый план не найден.", back)
        return

    client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
    market = MarketDataService(client, _market_cache)
    try:
        available = [info.symbol for info in await market.get_symbols()]
    except Exception as exc:  # noqa: BLE001
        await _reply(callback, _describe(exc), back)
        return
    finally:
        await client.close()

    matched, fixable, missing = _match_symbols(plan.allowed_symbols, available)
    if not fixable:
        await _reply(callback, "Исправлять нечего.", back)
        return

    # Потерянные инструменты сохраняем как есть: удалять их молча нельзя,
    # по ним может быть история сделок.
    plan.allowed_symbols = matched + list(fixable.values()) + missing
    await session.flush()

    changes = "\n".join(f"• {old} → {new}" for old, new in fixable.items())
    await _reply(
        callback,
        f"✅ <b>Названия исправлены</b>\n\n{changes}\n\n"
        f"Теперь эти инструменты доступны в ценах и анализе.",
        back,
    )
