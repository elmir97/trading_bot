"""Действия с открытой позицией (этап 4): расчёт карточки, без I/O.

Пять действий первого релиза: стоп в безубыток, стоп на свою цену, тейк
(поставить/изменить), закрыть 25% / 50%, закрыть всё. На вход — то, что уже
прочитано с биржи (позиция, её стопы и тейки из openOrders, mark price,
SymbolInfo) и журнала (комиссия входа, исходный стоп — база 1R), на выход —
план действия с цифрами карточки или отказ (ExecutionRefusal).

Правила разведки 02.10 (A/B/C): стоп и тейк — closePosition=true на всю
позицию (quantity формальный), после частичного закрытия не переставляются;
снятия стопа нет вовсе. Риск растёт (стоп дальше от входа) — только через
«Да, увеличить риск» и никогда выше потолка торгового плана
(risk_per_trade_percent от equity).

Деньги — Decimal полной точности, округление только у уровней (к шагу цены,
в сторону, не ухудшающую позицию) и у объёма (к шагу лота, вниз).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from app.core.numfmt import fmt_amount, fmt_money, fmt_pct, fmt_price, fmt_qty
from app.exchanges.base import Position, SymbolInfo
from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.position_view import ProtectiveOrder
from app.trading.enums import PositionActionKind as Kind
from app.trading.enums import TradeSide

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
R_STEP = Decimal("0.01")

# Доли частичного закрытия — значение callback «c25»/«c50».
CLOSE_FRACTIONS: dict[str, Decimal] = {"25": Decimal("0.25"), "50": Decimal("0.5")}

DISCLAIMER = "<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>"

KIND_TITLE = {
    Kind.MOVE_STOP: "перенос стопа",
    Kind.SET_TAKE: "тейк",
    Kind.CLOSE_PARTIAL: "частичное закрытие",
    Kind.CLOSE_FULL: "закрыть всё",
}


@dataclass(frozen=True, slots=True)
class ActionInputs:
    position: Position
    stops: tuple[ProtectiveOrder, ...]
    takes: tuple[ProtectiveOrder, ...]
    mark: Decimal
    symbol_info: SymbolInfo
    fee_rate: Decimal                       # taker на ногу (exec_taker_fee_rate)
    min_distance_percent: Decimal           # exec_min_stop_distance_percent
    # Комиссия входа на единицу объёма из журнала; None — оценка ставкой.
    entry_fee_per_unit: Decimal | None = None
    # |вход − исходный стоп| из журнала (Trade.risk_stop) — 1R на единицу.
    one_r_per_unit: Decimal | None = None
    equity: Decimal | None = None
    risk_cap_percent: Decimal | None = None  # TradingPlan.risk_per_trade_percent


@dataclass(frozen=True, slots=True)
class ActionPlan:
    kind: Kind
    params: dict[str, object]
    current_stop: Decimal | None
    current_take: Decimal | None
    new_level: Decimal | None = None
    close_qty: Decimal | None = None
    remainder: Decimal | None = None
    # Ордер, который снимается после подтверждения нового (перенос стопа/тейка).
    replaces_order_id: str | None = None
    # Убыток с комиссиями, если позиция закроется по стопу; None — стопа нет.
    risk_before: Decimal | None = None
    risk_after: Decimal | None = None
    r_before: Decimal | None = None
    r_after: Decimal | None = None
    fee: Decimal = ZERO
    pnl_estimate: Decimal | None = None
    risk_increase: bool = False
    notes: list[str] = field(default_factory=list)


# --- формулы -----------------------------------------------------------------


def _step(precision: int) -> Decimal:
    return ONE.scaleb(-precision)


def _sign(side: TradeSide) -> Decimal:
    return ONE if side is TradeSide.LONG else -ONE


def entry_fee_unit(inputs: ActionInputs) -> Decimal:
    """Комиссия входа на единицу: из журнала, иначе ставкой от цены входа."""
    if inputs.entry_fee_per_unit is not None:
        return inputs.entry_fee_per_unit
    return inputs.position.entry_price * inputs.fee_rate


def breakeven_price(
    side: TradeSide,
    entry: Decimal,
    fee_rate: Decimal,
    entry_fee_per_unit: Decimal,
    price_precision: int,
) -> Decimal:
    """Цена стопа, при которой выход даёт ≈0 нетто после комиссий входа и
    выхода. На единицу: LONG (X − E) − Fin − f·X = 0 → X = (E + Fin)/(1 − f);
    SHORT (E − X) − Fin − f·X = 0 → X = (E − Fin)/(1 + f). Fin = E·f при
    оценке ставкой — тогда LONG X = E·(1+f)/(1−f), SHORT X = E·(1−f)/(1+f).
    Округление к шагу цены — в сторону прибыли (LONG вверх, SHORT вниз),
    чтобы округление не увело нетто ниже нуля."""
    step = _step(price_precision)
    if side is TradeSide.LONG:
        return ((entry + entry_fee_per_unit) / (ONE - fee_rate)).quantize(step, ROUND_CEILING)
    return ((entry - entry_fee_per_unit) / (ONE + fee_rate)).quantize(step, ROUND_FLOOR)


def loss_at(inputs: ActionInputs, level: Decimal, qty: Decimal) -> Decimal:
    """Убыток объёма qty, если позиция закроется по level, с комиссией входа
    (доля) и выхода. Отрицательный — по этому уровню прибыль."""
    entry = inputs.position.entry_price
    gross = (entry - level) * qty * _sign(inputs.position.side)
    return gross + level * qty * inputs.fee_rate + entry_fee_unit(inputs) * qty


def _r(inputs: ActionInputs, loss: Decimal | None, qty: Decimal) -> Decimal | None:
    if loss is None or not inputs.one_r_per_unit or qty <= ZERO:
        return None
    return (loss / (inputs.one_r_per_unit * qty)).quantize(R_STEP, ROUND_HALF_UP)


def _single(orders: tuple[ProtectiveOrder, ...]) -> ProtectiveOrder | None:
    return orders[0] if len(orders) == 1 else None


def _refuse(code: Code, message: str) -> ExecutionRefusal:
    return ExecutionRefusal(code, message)


# --- план --------------------------------------------------------------------


def plan_action(
    kind: Kind, params: dict[str, object], inputs: ActionInputs
) -> ActionPlan | ExecutionRefusal:
    """План действия или отказ. params: {"breakeven": True} | {"level": "1.5"}
    (MOVE_STOP), {"level": "…"} (SET_TAKE), {"fraction": "25"|"50"}
    (CLOSE_PARTIAL), {} (CLOSE_FULL)."""
    p = inputs.position
    q = p.quantity
    precision = inputs.symbol_info.price_precision
    step = _step(precision)
    dist = inputs.mark * inputs.min_distance_percent / HUNDRED
    stop = _single(inputs.stops)
    take = _single(inputs.takes)
    current_stop = stop.trigger_price if stop else None
    current_take = take.trigger_price if take else None
    risk_now = loss_at(inputs, current_stop, q) if current_stop is not None else None
    long = p.side is TradeSide.LONG

    if kind is Kind.MOVE_STOP:
        if len(inputs.stops) > 1:
            return _refuse(
                Code.STOP_AMBIGUOUS, "На позиции несколько стопов — переставь их на бирже."
            )
        breakeven = bool(params.get("breakeven"))
        if breakeven:
            level = breakeven_price(
                p.side, p.entry_price, inputs.fee_rate, entry_fee_unit(inputs), precision
            )
        else:
            typed = _parse_level(params)
            if typed is None:
                return _refuse(Code.INVALID_PRICE, "Цена стопа — положительное число.")
            # К шагу цены — в сторону, не увеличивающую риск: LONG вверх, SHORT вниз.
            level = typed.quantize(step, ROUND_CEILING if long else ROUND_FLOOR)
        ok = level <= inputs.mark - dist if long else level >= inputs.mark + dist
        if not ok:
            if breakeven:
                return _refuse(
                    Code.BREAKEVEN_NOT_REACHED,
                    f"Безубыток {fmt_price(level, precision)}: mark "
                    f"{fmt_price(inputs.mark, precision)} ещё не ушёл за него на "
                    f"{fmt_pct(inputs.min_distance_percent)} — стоп сработал бы сразу.",
                )
            return _refuse(
                Code.STOP_WRONG_SIDE,
                f"Стоп {fmt_price(level, precision)} {'не ниже' if long else 'не выше'} mark "
                f"{fmt_price(inputs.mark, precision)} − {fmt_pct(inputs.min_distance_percent)} — "
                "сработал бы сразу.",
            )
        if current_stop == level:
            return _refuse(Code.INVALID_PRICE, f"Стоп уже стоит на {fmt_price(level, precision)}.")
        risk_after = loss_at(inputs, level, q)
        risk_increase = risk_now is not None and risk_after > risk_now
        if risk_increase:
            capped = _check_cap(inputs, risk_after)
            if capped is not None:
                return capped
        return ActionPlan(
            kind=kind,
            params={"breakeven": True} if breakeven else {"level": str(level)},
            current_stop=current_stop, current_take=current_take, new_level=level,
            replaces_order_id=stop.order_id if stop else None,
            risk_before=risk_now, risk_after=risk_after,
            r_before=_r(inputs, risk_now, q), r_after=_r(inputs, risk_after, q),
            fee=level * q * inputs.fee_rate, risk_increase=risk_increase,
        )

    if kind is Kind.SET_TAKE:
        if len(inputs.takes) > 1:
            return _refuse(
                Code.TAKE_AMBIGUOUS, "На позиции несколько тейков — переставь их на бирже."
            )
        typed_take = _parse_level(params)
        if typed_take is None:
            return _refuse(Code.INVALID_PRICE, "Цена тейка — положительное число.")
        level = typed_take.quantize(step, ROUND_HALF_UP)
        ok = level >= inputs.mark + dist if long else level <= inputs.mark - dist
        if not ok:
            return _refuse(
                Code.TAKE_WRONG_SIDE,
                f"Тейк {fmt_price(level, precision)} {'не выше' if long else 'не ниже'} mark "
                f"{fmt_price(inputs.mark, precision)} + {fmt_pct(inputs.min_distance_percent)} — "
                "сработал бы сразу.",
            )
        if current_take == level:
            return _refuse(Code.INVALID_PRICE, f"Тейк уже стоит на {fmt_price(level, precision)}.")
        return ActionPlan(
            kind=kind, params={"level": str(level)},
            current_stop=current_stop, current_take=current_take, new_level=level,
            replaces_order_id=take.order_id if take else None,
            risk_before=risk_now, risk_after=risk_now,
            r_before=_r(inputs, risk_now, q), r_after=_r(inputs, risk_now, q),
            fee=level * q * inputs.fee_rate, pnl_estimate=-loss_at(inputs, level, q),
        )

    if kind in (Kind.CLOSE_PARTIAL, Kind.CLOSE_FULL):
        info = inputs.symbol_info
        if kind is Kind.CLOSE_FULL:
            close_qty = q
        else:
            fraction = CLOSE_FRACTIONS.get(str(params.get("fraction")))
            if fraction is None:
                return _refuse(Code.INVALID_PRICE, "Доля закрытия — 25% или 50%.")
            close_qty = (q * fraction).quantize(_step(info.quantity_precision), ROUND_DOWN)
            if close_qty < info.min_quantity or close_qty * inputs.mark < info.min_notional:
                return _refuse(
                    Code.CLOSE_TOO_SMALL,
                    f"{params.get('fraction')}% позиции — "
                    f"{fmt_qty(close_qty, info.quantity_precision)},"
                    " меньше минимума биржи "
                    f"({fmt_qty(info.min_quantity, info.quantity_precision)}"
                    f" / {fmt_amount(info.min_notional)} USDT).",
                )
            rest = q - close_qty
            if rest > ZERO and (rest < info.min_quantity or rest * inputs.mark < info.min_notional):
                return _refuse(
                    Code.REMAINDER_TOO_SMALL,
                    f"Остаток {fmt_qty(rest, info.quantity_precision)} — меньше минимума биржи; "
                    "закрой позицию целиком.",
                )
        remainder = q - close_qty
        fee = close_qty * inputs.mark * inputs.fee_rate
        pnl = (
            (inputs.mark - p.entry_price) * close_qty * _sign(p.side)
            - fee - entry_fee_unit(inputs) * close_qty
        )
        rest_risk: Decimal | None = (
            loss_at(inputs, current_stop, remainder)
            if current_stop is not None and remainder > ZERO
            else (ZERO if remainder == ZERO else None)
        )
        return ActionPlan(
            kind=kind,
            params={"fraction": str(params.get("fraction"))} if kind is Kind.CLOSE_PARTIAL else {},
            current_stop=current_stop, current_take=current_take,
            close_qty=close_qty, remainder=remainder,
            risk_before=risk_now, risk_after=rest_risk,
            r_before=_r(inputs, risk_now, q), r_after=_r(inputs, rest_risk, remainder),
            fee=fee, pnl_estimate=pnl,
        )

    raise ValueError(f"Неизвестное действие: {kind}")


def levels_after_partial(
    stops: tuple[ProtectiveOrder, ...], takes: tuple[ProtectiveOrder, ...],
    remainder: Decimal, quantity_precision: int,
) -> str:
    """Что будет со стопом и тейком после частичного закрытия (08.10.2026).

    closePosition — «на всю позицию»; ордер на объём (вложенный во вход,
    ручной «на часть позиции») — BingX уменьшает его под остаток сам (Т0 08.10:
    стоп и тейк на 195 после закрытия 48 стали 147 в ту же секунду)."""

    def one(name: str, orders: tuple[ProtectiveOrder, ...]) -> str:
        if not orders:
            return f"{name}а нет"
        if all(o.close_position for o in orders):
            return f"{name} на всю позицию"
        sized = [o for o in orders if not o.close_position and o.quantity is not None]
        qty = ", ".join(fmt_qty(o.quantity, quantity_precision) for o in sized)
        return (
            f"{name} на {qty} — биржа уменьшит до "
            f"{fmt_qty(remainder, quantity_precision)}"
        )

    rest = fmt_qty(remainder, quantity_precision)
    if all(o.close_position for o in (*stops, *takes)) and stops and takes:
        return f"Остаток {rest} — стоп и тейк остаются на всю позицию"
    return f"Остаток {rest}: {one('стоп', stops)}; {one('тейк', takes)}"


def _parse_level(params: dict[str, object]) -> Decimal | None:
    try:
        level = Decimal(str(params.get("level")).strip().replace(",", "."))
    except (ArithmeticError, ValueError):
        return None
    if not level.is_finite() or level <= ZERO:
        return None
    return level


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _check_cap(inputs: ActionInputs, risk_after: Decimal) -> ExecutionRefusal | None:
    """Жёсткий потолок: риск растёт — не выше risk_per_trade_percent плана от
    equity. Даже после «Да, увеличить риск». Нет плана или equity — отказ."""
    if inputs.risk_cap_percent is None or inputs.equity is None or inputs.equity <= ZERO:
        return _refuse(
            Code.RISK_CAP_UNKNOWN,
            "Риск растёт, а потолок плана не проверить (нет торгового плана или баланса).",
        )
    cap = inputs.equity * inputs.risk_cap_percent / HUNDRED
    # 03.10.2026: сравниваются суммы в центах, как в тексте отказа — иначе
    # хвост деления давал отказ «20.00 USDT больше потолка 20.00 USDT».
    # Допуск не больше 0.005 USDT; потолок остаётся жёстким.
    if _cents(risk_after) > _cents(cap):
        return _refuse(
            Code.RISK_CAP_EXCEEDED,
            f"Новый риск {fmt_amount(risk_after)} USDT больше потолка плана "
            f"{fmt_pct(inputs.risk_cap_percent)} = {fmt_amount(cap)} USDT.",
        )
    return None


# --- текст карточки ----------------------------------------------------------


def _risk_text(loss: Decimal | None, r: Decimal | None) -> str:
    if loss is None:
        return "не ограничен (стопа нет)"
    if loss <= ZERO:
        text = f"нет, зафиксировано {fmt_amount(-loss)} USDT" if loss < ZERO else "≈0 USDT"
    else:
        text = f"{fmt_amount(loss)} USDT"
    return f"{text} ({r}R)" if r is not None else text


def render_card(plan: ActionPlan, inputs: ActionInputs) -> str:
    p = inputs.position
    pp = inputs.symbol_info.price_precision
    qp = inputs.symbol_info.quantity_precision
    title = KIND_TITLE[plan.kind]
    if plan.kind is Kind.MOVE_STOP and plan.params.get("breakeven"):
        title = "стоп в безубыток"
    lines = [
        f"<b>{p.symbol} {p.side.value} · {title}</b>",
        f"Позиция {fmt_qty(p.quantity, qp)} · вход {fmt_price(p.entry_price, pp)} · "
        f"mark {fmt_price(inputs.mark, pp)}",
    ]
    if plan.kind is Kind.MOVE_STOP:
        old = fmt_price(plan.current_stop, pp) if plan.current_stop is not None else "нет"
        lines.append(f"Стоп: {old} → <b>{fmt_price(plan.new_level, pp)}</b> (на всю позицию)")
    elif plan.kind is Kind.SET_TAKE:
        old = fmt_price(plan.current_take, pp) if plan.current_take is not None else "нет"
        lines.append(f"Тейк: {old} → <b>{fmt_price(plan.new_level, pp)}</b> (на всю позицию)")
        if plan.pnl_estimate is not None:
            lines.append(f"PnL по тейку ≈ {fmt_money(plan.pnl_estimate)} USDT (после комиссий)")
    else:
        what = "всю позицию" if plan.kind is Kind.CLOSE_FULL else f"{plan.params.get('fraction')}%"
        lines.append(
            f"Закрыть {what}: <b>{fmt_qty(plan.close_qty, qp)}</b> маркетом · "
            f"комиссия ≈{fmt_amount(plan.fee)} USDT"
        )
        if plan.pnl_estimate is not None:
            lines.append(f"PnL ≈ {fmt_money(plan.pnl_estimate)} USDT по mark (после комиссий)")
        if plan.kind is Kind.CLOSE_PARTIAL and plan.remainder is not None:
            lines.append(levels_after_partial(inputs.stops, inputs.takes, plan.remainder, qp))
    if plan.kind is not Kind.SET_TAKE:
        if plan.risk_increase:
            delta = (
                f" (+{(plan.r_after - plan.r_before).quantize(R_STEP)}R)"
                if plan.r_after is not None and plan.r_before is not None else ""
            )
            lines.append(
                f"⚠️ <b>Риск увеличится:</b> {_risk_text(plan.risk_before, None)} → "
                f"{_risk_text(plan.risk_after, None)}{delta}"
            )
        else:
            lines.append(
                f"Риск: {_risk_text(plan.risk_before, plan.r_before)} → "
                f"{_risk_text(plan.risk_after, plan.r_after)}"
            )
        if plan.kind is Kind.MOVE_STOP:
            lines.append(f"Комиссия выхода по стопу ≈{fmt_amount(plan.fee)} USDT (учтена в риске)")
        if plan.kind is Kind.MOVE_STOP and plan.params.get("breakeven"):
            lines.append("≈0 нетто без учёта проскальзывания (стоп исполняется маркетом)")
    if inputs.equity and plan.risk_after is not None and plan.risk_after > ZERO:
        share = (plan.risk_after / inputs.equity * HUNDRED).quantize(R_STEP, ROUND_HALF_UP)
        lines.append(f"Риск после — {share}% от {fmt_amount(inputs.equity)} USDT")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)
