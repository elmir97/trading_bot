"""Сверка журнала с биржей — решения без I/O (шаг 15.6, раздел 10 ТЗ).

Журнал — история, биржа — истина по текущему состоянию. Этот модуль только
решает по готовому снимку (сделки бота, позиции, история ордеров, ответ на
поиск входа), что сделать; запросы к бирже, запись в журнал, события и
уведомления — app/workers/reconciler.py. Так каждый случай проверяется
тестом на живых ответах демо без моков сети.

Правила:
- ордеров reconciler не отправляет никогда (ни вход, ни спасение стопа);
- сделки source=MANUAL/IMPORTED не трогает;
- выход пишется только однозначным фактом: закрывающие исполненные ордера
  биржи (сторона закрытия + positionSide сделки), чей объём ровно сходится
  с тем, насколько позиция уменьшилась; иначе — расхождение, журнал не
  правится;
- UNKNOWN/PENDING старше окна — поиск по client_order_id; «ордер не
  выставлен» (NOT_PLACED) — только если биржа ответила «order not exist» и
  позиции по символу и стороне нет.

Сработавший стоп/тейк на бирже — дочерний маркет-ордер: свой orderId,
triggerOrderId = orderId условника (снято живьём 27.09, SOL #4).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal

from app.exchanges.base import HistoryOrder, OpenOrder, OrderFill, Position
from app.trading.enums import ReconciliationKind, TradeSide
from app.trading.exit_reasons import (
    EXIT_MANUAL_STOP,
    EXIT_MANUAL_TAKE,
    EXIT_OUTSIDE_BOT,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
)

ZERO = Decimal(0)

# Окно, после которого UNKNOWN/PENDING разбирается (решение 27.09): заведомо
# длиннее TTL лока «Да» (173 с) — путь подтверждения к этому моменту либо
# завершён, либо процесс умер.
UNRESOLVED_ENTRY_WINDOW = timedelta(minutes=10)

# Ордер истории может получить updateTime на секунды раньше opened_at сделки
# (разные часы: время исполнения против времени ответа).
HISTORY_SKEW = timedelta(seconds=5)

# BingX: ордера с таким client_order_id нет (живьём 27.09, HTTP 200).
ORDER_NOT_EXIST_CODE = 109421

_CLOSING_SIDE = {TradeSide.LONG: "SELL", TradeSide.SHORT: "BUY"}
# Ручное закрытие позиции на бирже — обычный маркет или лимит без условника.
_PLAIN_ORDER_TYPES = frozenset({"MARKET", "LIMIT"})
# Условник, поставленный на бирже вручную (29.09, LINK #3): стоп или тейк по
# типу. Трейлинг и прочее — не угадываем, AMBIGUOUS.
_MANUAL_STOP_TYPES = frozenset({"STOP_MARKET", "STOP"})
_MANUAL_TAKE_TYPES = frozenset({"TAKE_PROFIT_MARKET", "TAKE_PROFIT"})
# Защита позиции для проверки STOP_MISSING: стоп бота или ручной, маркет или
# лимитный. Трейлинг — не защита (стоп плавает, уровня нет).
_PROTECTIVE_STOP_TYPES = _MANUAL_STOP_TYPES


@dataclass(frozen=True, slots=True, kw_only=True)
class BotTradeSnapshot:
    """Открытая сделка бота (SIGNAL_EXECUTION, fill_confirmed) для сверки."""

    trade_id: int
    symbol: str
    side: TradeSide
    open_quantity: Decimal
    opened_at: datetime
    stop_order_id: str | None
    take_order_id: str | None
    # orderId биржи, уже записанные исполнениями этой сделки (вход, выходы).
    recorded_order_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ExitFill:
    order_id: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    executed_at: datetime
    reason: str
    kind: ReconciliationKind
    # profit биржи по ордеру (allOrders), без комиссий — в trade_fills для
    # сверки PnL при полном закрытии.
    realized_pnl: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: ReconciliationKind
    dedup_key: str
    symbol: str
    detail: str
    trade_id: int | None = None
    execution_order_id: int | None = None


@dataclass(slots=True)
class TradeDecision:
    """Итог сверки одной сделки: выходы к записи или расхождение."""

    trade_id: int
    exits: list[ExitFill] = field(default_factory=list)
    closes_fully: bool = False
    discrepancy: Discrepancy | None = None


def exchange_position(
    positions: list[Position], symbol: str, side: TradeSide
) -> Position | None:
    return next((p for p in positions if p.symbol == symbol and p.side is side), None)


def needs_history(trade: BotTradeSnapshot, position: Position | None) -> bool:
    """Историю ордеров стоит тянуть, только если позиция уменьшилась или
    исчезла — в штатном цикле это один запрос позиций и ничего больше."""
    return position is None or position.quantity != trade.open_quantity


def _classify_exit(
    trade: BotTradeSnapshot, order: HistoryOrder
) -> tuple[str, ReconciliationKind] | None:
    linked = {order.order_id, order.trigger_order_id}
    if trade.stop_order_id is not None and trade.stop_order_id in linked:
        return EXIT_STOP_LOSS, ReconciliationKind.CLOSED_STOP_LOSS
    if trade.take_order_id is not None and trade.take_order_id in linked:
        return EXIT_TAKE_PROFIT, ReconciliationKind.CLOSED_TAKE_PROFIT
    # Сработал не наш условник (дочерний ордер: triggerOrderId — чужой
    # родитель, reduceOnly). Символ, positionSide, сторона закрытия, FILLED и
    # «после входа» уже отобраны в decide_trade; объём сверяется там же.
    if order.trigger_order_id is not None and order.reduce_only:
        if order.order_type in _MANUAL_STOP_TYPES:
            return EXIT_MANUAL_STOP, ReconciliationKind.CLOSED_OUTSIDE_BOT
        if order.order_type in _MANUAL_TAKE_TYPES:
            return EXIT_MANUAL_TAKE, ReconciliationKind.CLOSED_OUTSIDE_BOT
    if order.trigger_order_id is None and order.order_type in _PLAIN_ORDER_TYPES:
        return EXIT_OUTSIDE_BOT, ReconciliationKind.CLOSED_OUTSIDE_BOT
    return None


def decide_trade(
    trade: BotTradeSnapshot,
    position: Position | None,
    orders: list[HistoryOrder],
) -> TradeDecision:
    """Позиция уменьшилась/исчезла → закрывающие исполненные ордера после
    входа, ещё не записанные в сделку. Их объём обязан ровно совпасть с
    тем, насколько позиция уменьшилась; иначе — расхождение без правки."""
    decision = TradeDecision(trade_id=trade.trade_id)
    remaining_on_exchange = position.quantity if position is not None else ZERO
    if remaining_on_exchange > trade.open_quantity:
        decision.discrepancy = Discrepancy(
            ReconciliationKind.QUANTITY_MISMATCH, f"qty:{trade.trade_id}", trade.symbol,
            f"на бирже {remaining_on_exchange}, в журнале открыто {trade.open_quantity} — "
            "позицию увеличили вне бота",
            trade_id=trade.trade_id,
        )
        return decision
    expected_closed = trade.open_quantity - remaining_on_exchange
    if expected_closed == ZERO:
        return decision

    closing_side = _CLOSING_SIDE[trade.side]
    candidates = sorted(
        (
            o for o in orders
            if o.symbol == trade.symbol
            and o.position_side == trade.side.value
            and o.side == closing_side
            and o.status == "FILLED"
            and o.executed_qty > ZERO
            and o.updated_at >= trade.opened_at - HISTORY_SKEW
            and o.order_id not in trade.recorded_order_ids
        ),
        key=lambda o: o.updated_at,
    )
    exits: list[ExitFill] = []
    for order in candidates:
        classified = _classify_exit(trade, order)
        if classified is None:
            decision.discrepancy = Discrepancy(
                ReconciliationKind.AMBIGUOUS, f"ambiguous:{trade.trade_id}:{order.order_id}",
                trade.symbol,
                f"закрывающий ордер {order.order_id} типа {order.order_type} "
                f"(triggerOrderId {order.trigger_order_id or '—'}) не узнан — не стоп и не "
                "тейк бота, не ручной стоп/тейк, не ручной маркет/лимит",
                trade_id=trade.trade_id,
            )
            return decision
        reason, kind = classified
        exits.append(
            ExitFill(
                order_id=order.order_id, price=order.avg_price, quantity=order.executed_qty,
                fee=order.fee, executed_at=order.updated_at, reason=reason, kind=kind,
                realized_pnl=order.realized_pnl,
            )
        )

    closed = sum((e.quantity for e in exits), ZERO)
    if closed != expected_closed:
        decision.discrepancy = Discrepancy(
            ReconciliationKind.QUANTITY_MISMATCH, f"qty:{trade.trade_id}", trade.symbol,
            f"позиция уменьшилась на {expected_closed}, закрывающих исполнений на {closed} — "
            "журнал не правлю",
            trade_id=trade.trade_id,
        )
        return decision

    decision.exits = exits
    decision.closes_fully = remaining_on_exchange == ZERO
    if not decision.closes_fully:
        for i, e in enumerate(exits):
            exits[i] = replace(e, kind=ReconciliationKind.PARTIAL_CLOSE)
    return decision


# Допуск сверки PnL — доля 1R (решение 28.09).
PNL_TOLERANCE_R = Decimal("0.01")


def pnl_mismatch(
    *,
    trade_id: int,
    symbol: str,
    journal_pnl: Decimal,
    fees: Decimal,
    exits_realized_pnl: list[Decimal],
    risk_amount: Decimal,
) -> Discrepancy | None:
    """Полностью закрытая сделка: PnL журнала против profit биржи по всем
    выходам минус все комиссии (вход и выходы — trade.fees). Расхождение
    больше PNL_TOLERANCE_R × 1R — аномалия, журнал не правится.

    Выходы без profit биржи (ручные, до 28.09) и неизвестный 1R вызывающий
    отсекает сам — сюда приходят только сравнимые числа."""
    tolerance = risk_amount * PNL_TOLERANCE_R
    exchange_pnl = sum(exits_realized_pnl, ZERO) - fees
    diff = journal_pnl - exchange_pnl
    if abs(diff) <= tolerance:
        return None
    return Discrepancy(
        ReconciliationKind.PNL_MISMATCH, f"pnl:{trade_id}", symbol,
        f"PnL журнала {_num(journal_pnl)}, по бирже {_num(exchange_pnl)} (profit "
        f"{_num(sum(exits_realized_pnl, ZERO))} − комиссии {_num(fees)}), разница "
        f"{_num(diff)} больше {PNL_TOLERANCE_R}R ({_num(tolerance)})",
        trade_id=trade_id,
    )


def _num(value: Decimal) -> str:
    text = f"{value.quantize(Decimal('0.00000001')):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def stop_missing(
    trade: BotTradeSnapshot, position: Position | None, open_orders: list[OpenOrder]
) -> Discrepancy | None:
    """Позиция бота есть, а закрывающего стопа по ней в openOrders нет —
    тревога уровня «ПОЗИЦИЯ БЕЗ СТОПА» (15.5.3). Ордеров не ставим.

    Стоп — любой закрывающий STOP_MARKET или STOP на позиции, не только стоп
    бота по id: ручной стоп владельца тоже защита (29.09). Трейлинг — нет."""
    if position is None:
        return None
    closing_side = _CLOSING_SIDE[trade.side]
    has_stop = any(
        o.symbol == trade.symbol
        and o.order_type in _PROTECTIVE_STOP_TYPES
        and o.position_side == trade.side.value
        and o.side == closing_side
        and o.stop_price is not None
        for o in open_orders
    )
    if has_stop:
        return None
    return Discrepancy(
        ReconciliationKind.STOP_MISSING, f"stop_missing:{trade.trade_id}", trade.symbol,
        f"позиция {trade.side.value} {position.quantity} открыта, стопа в openOrders нет",
        trade_id=trade.trade_id,
    )


def orphan_positions(
    positions: list[Position],
    journal_open: set[tuple[str, TradeSide]],
    in_flight: set[str],
) -> list[Discrepancy]:
    """Позиция на бирже, которой нет среди открытых сделок журнала (любого
    источника) по символу и стороне. Символы с входом «в полёте» (лок «Да»,
    незакрытая строка моложе окна) пропускаются: сделку ещё пишут."""
    found: list[Discrepancy] = []
    for p in positions:
        if (p.symbol, p.side) in journal_open or p.symbol in in_flight:
            continue
        found.append(
            Discrepancy(
                ReconciliationKind.ORPHAN_POSITION,
                f"orphan:{p.symbol}:{p.side.value}:{p.position_id or '—'}",
                p.symbol,
                f"позиция {p.side.value} {p.quantity} по {p.entry_price} на бирже, "
                "сделки в журнале нет — не создаю",
            )
        )
    return found


@dataclass(frozen=True, slots=True, kw_only=True)
class UnresolvedEntry:
    execution_order_id: int
    client_order_id: str
    symbol: str
    side: TradeSide
    status: str
    created_at: datetime
    trade_id: int | None


@dataclass(frozen=True, slots=True)
class EntryResolution:
    """confirmed — вход найден исполненным (fill), not_placed — окончательно
    «ордер не выставлен»; иначе discrepancy."""

    confirmed: OrderFill | None = None
    not_placed: bool = False
    discrepancy: Discrepancy | None = None


def entry_is_due(entry: UnresolvedEntry, now: datetime) -> bool:
    return now - entry.created_at >= UNRESOLVED_ENTRY_WINDOW


def resolve_entry(
    entry: UnresolvedEntry,
    lookup: OrderFill | None,
    *,
    not_found: bool,
    position: Position | None,
) -> EntryResolution:
    """lookup — ответ поиска по client_order_id; not_found — биржа ответила
    109421. Повторной отправки нет ни в одной ветке."""
    if lookup is not None:
        if lookup.status == "FILLED" and lookup.executed_qty > ZERO:
            return EntryResolution(confirmed=lookup)
        return EntryResolution(
            discrepancy=Discrepancy(
                ReconciliationKind.AMBIGUOUS, f"entry:{entry.execution_order_id}", entry.symbol,
                f"вход {entry.client_order_id} найден в статусе {lookup.status}, исполнено "
                f"{lookup.executed_qty} — журнал не правлю",
                trade_id=entry.trade_id, execution_order_id=entry.execution_order_id,
            )
        )
    if not_found and position is None:
        return EntryResolution(not_placed=True)
    return EntryResolution(
        discrepancy=Discrepancy(
            ReconciliationKind.AMBIGUOUS, f"entry:{entry.execution_order_id}", entry.symbol,
            (
                f"вход {entry.client_order_id} не найден, а позиция {entry.side.value} "
                "по символу есть — вывод не делаю"
                if not_found
                else f"вход {entry.client_order_id}: ответ биржи не разобран — жду"
            ),
            trade_id=entry.trade_id, execution_order_id=entry.execution_order_id,
        )
    )
