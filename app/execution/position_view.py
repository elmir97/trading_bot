"""Экран «Позиции» (этап 3): позиции с биржи, их стоп/тейк и связь с журналом.

Без I/O: на вход — то, что уже отдала биржа (get_positions, get_open_orders
без символа — один запрос на все символы) и открытые сделки журнала.

Стоп и тейк позиции — закрывающие условные ордера из openOrders по символу,
positionSide и стороне закрытия. Ордер с closePosition закрывает весь остаток
позиции (разведка 02.10: стоп с quantity 40 при позиции 30 исполнил 30) —
поэтому его quantity формальный и как объём не показывается: «на всю
позицию». У ордера без closePosition объём настоящий.

orderId и positionId — строкой везде: значения больше 2^53.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.numfmt import fmt_money, fmt_price, fmt_qty
from app.database.models.trade import Trade
from app.exchanges.base import OpenOrder, Position
from app.trading.enums import TradeSide

STOP_TYPES = frozenset({"STOP_MARKET", "STOP"})
TAKE_TYPES = frozenset({"TAKE_PROFIT_MARKET", "TAKE_PROFIT"})
_CLOSING_SIDE = {TradeSide.LONG: "SELL", TradeSide.SHORT: "BUY"}


@dataclass(frozen=True, slots=True)
class ProtectiveOrder:
    """Стоп или тейк позиции на бирже."""

    order_id: str
    trigger_price: Decimal
    # True — закрывает весь остаток; quantity тогда формальный, не объём.
    close_position: bool
    quantity: Decimal | None   # None при close_position
    working_type: str


@dataclass(frozen=True, slots=True)
class PositionView:
    position: Position
    stops: tuple[ProtectiveOrder, ...]
    takes: tuple[ProtectiveOrder, ...]
    trade: Trade | None   # открытая сделка журнала по символу и стороне

    @property
    def stop(self) -> ProtectiveOrder | None:
        """Единственный стоп; при нескольких (ручная лестница) — None."""
        return self.stops[0] if len(self.stops) == 1 else None

    @property
    def take(self) -> ProtectiveOrder | None:
        return self.takes[0] if len(self.takes) == 1 else None


def _protective(order: OpenOrder) -> ProtectiveOrder:
    return ProtectiveOrder(
        order_id=str(order.order_id),
        trigger_price=order.stop_price,  # type: ignore[arg-type]  # отобрано по stop_price
        close_position=order.close_position,
        quantity=None if order.close_position else order.quantity,
        working_type=order.working_type,
    )


def protective_orders(
    position: Position, open_orders: list[OpenOrder]
) -> tuple[tuple[ProtectiveOrder, ...], tuple[ProtectiveOrder, ...]]:
    """(стопы, тейки) позиции — от ближнего к цене к дальнему."""
    closing = _CLOSING_SIDE[position.side]
    mine = [
        o for o in open_orders
        if o.symbol == position.symbol
        and o.position_side == position.side.value
        and o.side == closing
        and o.stop_price is not None
    ]

    def by_distance(orders: list[OpenOrder]) -> tuple[ProtectiveOrder, ...]:
        ranked = sorted(orders, key=lambda o: abs(o.stop_price - position.mark_price))  # type: ignore[operator]
        return tuple(_protective(o) for o in ranked)

    return (
        by_distance([o for o in mine if o.order_type in STOP_TYPES]),
        by_distance([o for o in mine if o.order_type in TAKE_TYPES]),
    )


def link_trade(position: Position, open_trades: list[Trade]) -> Trade | None:
    """Открытая сделка журнала для позиции: по символу и стороне; при
    нескольких — с тем же positionId, иначе самая поздняя."""
    candidates = [
        t for t in open_trades if t.symbol == position.symbol and t.side is position.side
    ]
    if not candidates:
        return None
    if position.position_id is not None:
        for trade in candidates:
            if trade.external_position_id == position.position_id:
                return trade
    return max(candidates, key=lambda t: t.opened_at)


def build_views(
    positions: list[Position], open_orders: list[OpenOrder], open_trades: list[Trade]
) -> list[PositionView]:
    views = []
    for position in sorted(positions, key=lambda p: (p.symbol, p.side.value)):
        stops, takes = protective_orders(position, open_orders)
        views.append(PositionView(position, stops, takes, link_trade(position, open_trades)))
    return views


def journal_only(open_trades: list[Trade], views: list[PositionView]) -> list[Trade]:
    """Открытые сделки журнала без позиции на бирже (ручные записи) — их
    закрывают как раньше, вводом цены выхода."""
    linked = {v.trade.id for v in views if v.trade is not None}
    return [t for t in open_trades if t.id not in linked]


# --- текст -------------------------------------------------------------------


def _level_text(orders: tuple[ProtectiveOrder, ...], price_precision: int | None) -> str:
    if not orders:
        return "нет"

    def one(o: ProtectiveOrder) -> str:
        price = fmt_price(o.trigger_price, price_precision)
        if o.close_position:
            return f"{price} (на всю позицию)"
        return f"{price} ({fmt_qty(o.quantity)})"

    if len(orders) == 1:
        return one(orders[0])
    return f"{len(orders)} ордера: " + "; ".join(one(o) for o in orders)


def render_position(view: PositionView, price_precision: int | None) -> str:
    p = view.position
    icon = "🟢" if p.side is TradeSide.LONG else "🔴"
    lines = [
        f"{icon} <b>{p.symbol}</b> {p.side.value} · {fmt_qty(p.quantity)} · плечо {p.leverage}x",
        f"Вход {fmt_price(p.entry_price, price_precision)} · mark "
        f"{fmt_price(p.mark_price, price_precision)} · PnL {fmt_money(p.unrealized_pnl)} USDT",
        f"Стоп: {_level_text(view.stops, price_precision)}",
        f"Тейк: {_level_text(view.takes, price_precision)}",
    ]
    if p.liquidation_price:
        lines.append(f"Ликвидация: {fmt_price(p.liquidation_price, price_precision)}")
    if view.trade is not None:
        lines.append(f"📒 В журнале: сделка #{view.trade.id}")
    else:
        lines.append("⚠️ Не в журнале")
    return "\n".join(lines)


def render_journal_trade(trade: Trade) -> str:
    entry = fmt_price(trade.entry_price) if trade.entry_price is not None else "—"
    return f"📒 #{trade.id} {trade.symbol} {trade.side.value} · вход {entry}"


# --- синхронизация уровней журнала с биржей (reconciler) ---------------------


def tracks_exchange(trade: Trade, open_quantity: Decimal, position: Position | None) -> bool:
    """Импортированная или ручная сделка сверяется с биржей (этап 3), если в
    журнале по ней открыт объём и она связана с биржей: позиция по символу и
    стороне есть сейчас или у сделки есть positionId биржи (позиция могла
    закрыться). Ручная запись без позиции на бирже — только журнал."""
    if open_quantity <= 0:
        return False
    return trade.external_position_id is not None or position is not None


def apply_exchange_levels(
    trade: Trade,
    stops: tuple[ProtectiveOrder, ...],
    takes: tuple[ProtectiveOrder, ...],
) -> list[str]:
    """Стоп и тейк журнала → уровни на бирже. Возвращает список изменений
    для лога; пустой — ничего не менялось.

    Только при единственном ордере: лестница из нескольких стопов/тейков
    журнал не правит (какой из них «тот самый» — неизвестно). Нет ордера — не
    стираем: стоп сняли или он сработал, разберётся сверка выходов. Первый
    известный стоп остаётся в initial_stop_loss — база 1R (Trade.risk_stop).
    Новый уровень — новая отметка приближения (уведомление снова возможно)."""
    changes: list[str] = []
    if len(stops) == 1 and stops[0].trigger_price != trade.stop_loss:
        new = stops[0].trigger_price
        if trade.initial_stop_loss is None:
            trade.initial_stop_loss = trade.stop_loss if trade.stop_loss is not None else new
        changes.append(f"стоп {trade.stop_loss} → {new}")
        trade.stop_loss = new
        trade.sl_approach_notified_at = None
    if len(takes) == 1 and takes[0].trigger_price != trade.take_profit:
        new = takes[0].trigger_price
        changes.append(f"тейк {trade.take_profit} → {new}")
        trade.take_profit = new
        trade.tp_approach_notified_at = None
    return changes
