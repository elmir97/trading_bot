"""Вкладка «Позиции» Mini App (П.1) и линии позиции на экране рынка (П.2) — только чтение.

Данные — тем же путём, что экран «💼 Позиции» бота (app/bot/handlers/positions.py):
get_positions + get_open_orders без символа (только если позиции есть) →
build_views / journal_only (app/execution/position_view.py), точность цен — из
списка контрактов (кэш 1 ч). Счёт — bingx_allowed_exchange_mode, как у /api/me,
/api/market и открытия (решение владельца 17.10); выбранный в боте счёт
(user.settings.active_exchange_mode) только показывается, если расходится.
Сделки без счёта (NULL) и другого счёта с позицией не связываются (link_trade).

Лимиты BingX: positions — 10/с, openOrders — 5/с на ключ, ключ общий с ботом
(монитор, сверка). Троттлер BingX живёт в клиенте, а клиенты API одноразовые —
защищает кэш процесса: (пользователь, счёт) → позиции и ордера вместе на
TTL_POSITIONS, промах под локом ключа (TTLCache) — один запрос на всех. Итог
«Открыть» и отмена лимита сбрасывают запись (forget), иначе до TTL_POSITIONS
после открытия вкладка и линии показывали бы «позиции нет».

Клиент — ExchangeReads: только get_*, запись на биржу отсюда невозможна.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.api.deps import ApiError, ApiState
from app.api.market import MarketReader
from app.api.schemas import (
    BarOut,
    ErrorBody,
    JournalTradeOut,
    PositionOut,
    PositionsOut,
    ProtectiveOut,
    money,
    price,
)
from app.core.logging import get_logger
from app.database.models.trade import Trade
from app.database.models.user import User
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import ExchangeAuthError, ExchangeError, OpenOrder, Position
from app.execution.position_view import (
    PositionView,
    ProtectiveOrder,
    build_views,
    journal_only,
    start_param,
)
from app.trading.enums import TradeSide

logger = get_logger(__name__)

TTL_POSITIONS = 5
NO_KEY_TEXT = "🔑 Ключи биржи не подключены — показан только журнал."
KEY_REJECTED_TEXT = "🔑 Ключ биржи отклонён — проверь в настройках бота. Показан только журнал."


class ExchangeReads:
    """Клиент вкладки: только чтение (get_*); запись — AttributeError."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("get_"):
            raise AttributeError(f"«Позиции» не пишут на биржу: {name}")
        return getattr(self._client, name)

    async def close(self) -> None:
        await self._client.close()


@dataclass(frozen=True, slots=True)
class Snapshot:
    positions: tuple[Position, ...]
    orders: tuple[OpenOrder, ...]


def cache_key(state: ApiState, user_id: int) -> str:
    return f"api:positions:{user_id}:{state.settings.bingx_allowed_exchange_mode.value}"


def forget(state: ApiState, user_id: int) -> None:
    """Позиции изменились нашим действием (итог «Открыть», отмена лимита, «Да» экрана 5)."""
    state.market_cache.invalidate(f"api:positions:{user_id}:")


async def _snapshot(state: ApiState, session: Any, user_id: int) -> Snapshot:
    mode = state.settings.bingx_allowed_exchange_mode

    async def fetch() -> Snapshot:
        client = ExchangeReads(await state.factory.for_user(session, user_id, mode=mode))
        try:
            positions = await client.get_positions()
            orders = await client.get_open_orders() if positions else []
        finally:
            await client.close()
        return Snapshot(tuple(positions), tuple(orders))

    snapshot: Snapshot = await state.market_cache.get_or_fetch(
        cache_key(state, user_id), TTL_POSITIONS, fetch
    )
    return snapshot


def _protective(order: ProtectiveOrder, precision: int | None) -> ProtectiveOut:
    return ProtectiveOut(price=_px(order.trigger_price, precision),
                         close_position=order.close_position, quantity=order.quantity)


def _px(value: Any, precision: int | None) -> Any:
    # Контракта нет в списке (снят с торгов) — цена как отдала биржа, без округления.
    return value if precision is None or value is None else price(value, precision)


BAR_STEP = Decimal("0.0001")


def _plain(value: Decimal) -> Decimal:
    """Без хвоста нулей и без экспоненты: 2400.000000000000 → 2400 (контракта нет в списке)."""
    return Decimal(format(value.normalize(), "f"))


def _at(value: Decimal) -> Decimal:
    return min(max(value, Decimal(0)), Decimal(1)).quantize(BAR_STEP, rounding=ROUND_HALF_UP)


def position_bar(side: TradeSide, entry: Decimal, mark: Decimal, stop: Decimal | None,
                 take: Decimal | None, precision: int | None) -> BarOut | None:
    """Полоска стоп — вход — тейк (Д.6, решение владельца 18.10): доля от стопа (0) к тейку
    (1) — одна формула для обеих сторон: у шорта стоп выше тейка, шкала зеркальная сама.
    Нет стопа или тейка, или они не по своим сторонам (лонг: тейк выше стопа, шорт: ниже)
    — полоски нет. Вход и цена обрезаны по краям; цена за краем — mark_edge."""
    if stop is None or take is None:
        return None
    span = take - stop
    if (side is TradeSide.LONG and span <= 0) or (side is TradeSide.SHORT and span >= 0):
        return None
    mark_raw = (mark - stop) / span
    edge = "stop" if mark_raw < 0 else "take" if mark_raw > 1 else None

    def text(value: Decimal) -> str:
        return str(_px(value, precision))

    return BarOut(stop=f"стоп {text(stop)}", entry=f"вход {text(entry)}",
                  take=f"тейк {text(take)}", mark=text(mark),
                  entry_at=_at((entry - stop) / span), mark_at=_at(mark_raw), mark_edge=edge)


def pnl_text(value: Decimal | None) -> tuple[str | None, int]:
    """PnL текстом со знаком и знак для цвета (Д.6): плюс — «+», минус — «-», ноль — без."""
    if value is None:
        return None, 0
    v = money(value)
    assert v is not None
    sign = 1 if v > 0 else -1 if v < 0 else 0
    if sign == 0:
        v = abs(v)                  # −0.004 → «-0.00» после округления: ноль без минуса
    return f"{'+' if sign > 0 else ''}{v} $", sign


def position_out(view: PositionView, precision: int | None) -> PositionOut:
    p = view.position
    text, sign = pnl_text(p.unrealized_pnl)
    return PositionOut(
        symbol=p.symbol, side=p.side.value, quantity=p.quantity, leverage=p.leverage,
        entry_price=_px(p.entry_price, precision), mark_price=_px(p.mark_price, precision),
        unrealized_pnl=money(p.unrealized_pnl), pnl_text=text, pnl_sign=sign,
        liquidation_price=_px(p.liquidation_price or None, precision),
        price_precision=precision,
        bar=position_bar(p.side, p.entry_price, p.mark_price,
                         view.stops[0].trigger_price if view.stops else None,
                         view.takes[0].trigger_price if view.takes else None, precision),
        stops=[_protective(o, precision) for o in view.stops],
        takes=[_protective(o, precision) for o in view.takes],
        trade_id=view.trade.id if view.trade is not None else None,
        start_param=start_param(p.symbol, p.side),
    )


def journal_price(value: Decimal | None, precision: int | None) -> Decimal | None:
    """Вход сделки журнала (Д.6): по точности контракта; контракта нет в списке (снят,
    список не прочитан) — без хвоста нулей, не «2400.000000000000»."""
    if value is None:
        return None
    return price(value, precision) if precision is not None else _plain(value)


def _journal(trade: Trade, precision: dict[str, int]) -> JournalTradeOut:
    return JournalTradeOut(
        id=trade.id, symbol=trade.symbol, side=trade.side.value,
        entry_price=journal_price(trade.entry_price, precision.get(trade.symbol)),
        account_mode=trade.account_mode.value if trade.account_mode is not None else None,
    )


async def _journal_precision(state: ApiState, user_id: int,
                             trades: list[Trade]) -> dict[str, int]:
    """Точность цен для «Только в журнале» без ключа биржи (список контрактов публичный).
    Сбой списка — {}: вход без хвоста нулей, вкладка не падает (журнал важнее точности)."""
    if not trades:
        return {}
    try:
        async with MarketReader(state) as reader:
            return {i.symbol: i.price_precision for i in await reader.symbols()}
    except ExchangeError as exc:
        logger.warning("Mini App: список контрактов не прочитан — журнал без точности",
                       extra={"user_id": user_id, "error": type(exc).__name__})
        return {}


async def positions_out(state: ApiState, session: Any, user: User) -> PositionsOut:
    mode = state.settings.bingx_allowed_exchange_mode
    open_trades = await TradeRepository(session).list_open(user.id)

    async def journal_answer(code: str, message: str) -> PositionsOut:
        # Без биржи все открытые сделки — «только журнал» (как в боте).
        precision = await _journal_precision(state, user.id, open_trades)
        return PositionsOut(
            account_mode=mode.value, bot_account_mode=user.settings.active_exchange_mode.value,
            positions=[], journal_only=[_journal(t, precision) for t in open_trades],
            exchange_error=ErrorBody(code=code, message=message),
        )

    if await state.factory.get_credentials(session, user.id, mode=mode) is None:
        return await journal_answer("NO_KEY", NO_KEY_TEXT)
    try:
        snapshot = await _snapshot(state, session, user.id)
        precision: dict[str, int] = {}
        if snapshot.positions:
            async with MarketReader(state) as reader:
                precision = {i.symbol: i.price_precision for i in await reader.symbols()}
    except ExchangeAuthError:
        logger.warning("Mini App: биржа отклонила ключ при чтении позиций",
                       extra={"user_id": user.id})
        return await journal_answer("KEY_REJECTED", KEY_REJECTED_TEXT)
    except ExchangeError as exc:
        logger.warning("Mini App: позиции не прочитаны",
                       extra={"user_id": user.id, "error": type(exc).__name__})
        raise ApiError(503, "EXCHANGE_UNAVAILABLE",
                       f"Биржа не ответила ({type(exc).__name__}) — повтори.") from None
    views = build_views(list(snapshot.positions), list(snapshot.orders), open_trades,
                        account_mode=mode)
    journal = journal_only(open_trades, views)
    # Точность журнала: список уже прочитан для позиций — он же; нет позиций — отдельно, сбой
    # списка вкладку не роняет (_journal_precision).
    journal_precision = precision if precision else await _journal_precision(state, user.id,
                                                                             journal)
    return PositionsOut(
        account_mode=mode.value, bot_account_mode=user.settings.active_exchange_mode.value,
        positions=[position_out(v, precision.get(v.position.symbol)) for v in views],
        journal_only=[_journal(t, journal_precision) for t in journal],
        exchange_error=None,
    )


__all__ = ["TTL_POSITIONS", "ExchangeReads", "cache_key", "forget", "journal_price", "pnl_text",
           "position_bar", "position_out", "positions_out"]
