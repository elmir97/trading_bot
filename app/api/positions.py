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
from typing import Any

from app.api.deps import ApiError, ApiState
from app.api.market import MarketReader
from app.api.schemas import (
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
    """Позиции изменились нашим действием (итог «Открыть», отмена лимита)."""
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


def _position(view: PositionView, precision: int | None) -> PositionOut:
    p = view.position
    return PositionOut(
        symbol=p.symbol, side=p.side.value, quantity=p.quantity, leverage=p.leverage,
        entry_price=_px(p.entry_price, precision), mark_price=_px(p.mark_price, precision),
        unrealized_pnl=money(p.unrealized_pnl),
        liquidation_price=_px(p.liquidation_price or None, precision),
        price_precision=precision,
        stops=[_protective(o, precision) for o in view.stops],
        takes=[_protective(o, precision) for o in view.takes],
        trade_id=view.trade.id if view.trade is not None else None,
        start_param=start_param(p.symbol, p.side),
    )


def _journal(trade: Trade) -> JournalTradeOut:
    return JournalTradeOut(
        id=trade.id, symbol=trade.symbol, side=trade.side.value, entry_price=trade.entry_price,
        account_mode=trade.account_mode.value if trade.account_mode is not None else None,
    )


async def positions_out(state: ApiState, session: Any, user: User) -> PositionsOut:
    mode = state.settings.bingx_allowed_exchange_mode
    open_trades = await TradeRepository(session).list_open(user.id)

    def journal_answer(code: str, message: str) -> PositionsOut:
        # Без биржи все открытые сделки — «только журнал» (как в боте).
        return PositionsOut(
            account_mode=mode.value, bot_account_mode=user.settings.active_exchange_mode.value,
            positions=[], journal_only=[_journal(t) for t in open_trades],
            exchange_error=ErrorBody(code=code, message=message),
        )

    if await state.factory.get_credentials(session, user.id, mode=mode) is None:
        return journal_answer("NO_KEY", NO_KEY_TEXT)
    try:
        snapshot = await _snapshot(state, session, user.id)
        precision: dict[str, int] = {}
        if snapshot.positions:
            async with MarketReader(state) as reader:
                precision = {i.symbol: i.price_precision for i in await reader.symbols()}
    except ExchangeAuthError:
        logger.warning("Mini App: биржа отклонила ключ при чтении позиций",
                       extra={"user_id": user.id})
        return journal_answer("KEY_REJECTED", KEY_REJECTED_TEXT)
    except ExchangeError as exc:
        logger.warning("Mini App: позиции не прочитаны",
                       extra={"user_id": user.id, "error": type(exc).__name__})
        raise ApiError(503, "EXCHANGE_UNAVAILABLE",
                       f"Биржа не ответила ({type(exc).__name__}) — повтори.") from None
    views = build_views(list(snapshot.positions), list(snapshot.orders), open_trades,
                        account_mode=mode)
    return PositionsOut(
        account_mode=mode.value, bot_account_mode=user.settings.active_exchange_mode.value,
        positions=[_position(v, precision.get(v.position.symbol)) for v in views],
        journal_only=[_journal(t) for t in journal_only(open_trades, views)],
        exchange_error=None,
    )


__all__ = ["TTL_POSITIONS", "ExchangeReads", "cache_key", "forget", "positions_out"]
