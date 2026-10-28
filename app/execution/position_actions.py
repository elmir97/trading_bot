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

Деньги — Decimal полной точности, округление только у уровней и у объёма
(к шагу лота, вниз). Уровни — к шагу цены по правилу app/execution/price_step
(A.4): объём фиксирован, поэтому стоп — ближе к цене (риск в $ не растёт),
тейк — ближе.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from app.core.numfmt import (
    fmt_amount,
    fmt_fee,
    fmt_money,
    fmt_pct,
    fmt_price,
    fmt_qty,
    fmt_small_pct,
)
from app.exchanges.base import Position, SymbolInfo
from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.position_view import ProtectiveOrder
from app.execution.price_step import stop_closer, take_closer
from app.trading.enums import FillSide, TradeSide
from app.trading.enums import PositionActionKind as Kind

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
    return entry_fee_or_rate(inputs.entry_fee_per_unit, inputs.position.entry_price,
                             inputs.fee_rate)


def entry_fee_or_rate(journal_unit: Decimal | None, entry: Decimal, fee_rate: Decimal) -> Decimal:
    """Комиссия входа на единицу — журнала, иначе ставкой от входа. Одна для карточки «Стоп в
    безубыток» и монитора позиций (К9): безубыток у них — одна цена."""
    return journal_unit if journal_unit is not None else entry * fee_rate


def journal_entry_fee_unit(fills: Iterable[Any]) -> Decimal | None:
    """Комиссия входа на единицу по исполнениям журнала (fill_side ENTRY): сумма комиссий /
    объём входа; входа нет — None (тогда ставкой, entry_fee_or_rate)."""
    entries = [f for f in fills if f.fill_side is FillSide.ENTRY]
    qty = sum((f.quantity for f in entries), ZERO)
    if qty <= ZERO:
        return None
    return sum((f.fee for f in entries), ZERO) / qty


def stop_secures_breakeven(side: TradeSide, stop: Decimal, breakeven: Decimal) -> bool:
    """Стоп в безубытке или в прибыли (К9): LONG — не ниже безубытка, SHORT — не выше; ровно на
    безубытке — тоже (сработав, убытка не даст). Алерт «приближается к стопу» тогда не шлётся."""
    return stop >= breakeven if side is TradeSide.LONG else stop <= breakeven


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


def stop_clear_of_mark(
    level: Decimal, mark: Decimal, min_distance_percent: Decimal, side: TradeSide
) -> bool:
    """Стоп level не сработает сразу: LONG — не выше mark − дистанция, SHORT — не ниже
    mark + дистанция (дистанция — min_distance_percent от mark). Одна формула для плана и
    порога безубытка."""
    dist = mark * min_distance_percent / HUNDRED
    return level <= mark - dist if side is TradeSide.LONG else level >= mark + dist


def breakeven_threshold(
    side: TradeSide, level: Decimal, min_distance_percent: Decimal, precision: int
) -> Decimal:
    """Mark на шаге цены, начиная с которого стоп на level проходит stop_clear_of_mark (К4,
    28.10.2026): LONG — level / (1 − m) вверх до шага, SHORT — level / (1 + m) вниз (m — доля).
    Только Decimal; затем шагом до границы по той же проверке, что у плана: при mark = порогу
    стоп проходит, на шаг хуже — нет, без зазора от округления деления."""
    step = _step(precision)
    m = min_distance_percent / HUNDRED
    long = side is TradeSide.LONG
    worse = -step if long else step
    if long:
        x = (level / (ONE - m)).quantize(step, ROUND_CEILING)
    else:
        x = (level / (ONE + m)).quantize(step, ROUND_FLOOR)
    while not stop_clear_of_mark(level, x, min_distance_percent, side):
        x -= worse
    while stop_clear_of_mark(level, x + worse, min_distance_percent, side):
        x += worse
    return x


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
    | {"level_percent": "1"} — отступ от mark (MOVE_STOP), {"level": "…"}
    (SET_TAKE), {"fraction": "25"|"50"} (CLOSE_PARTIAL), {} (CLOSE_FULL)."""
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
        percent: Decimal | None = None
        if breakeven:
            level = breakeven_price(
                p.side, p.entry_price, inputs.fee_rate, entry_fee_unit(inputs), precision
            )
        else:
            typed = _parse_level(params)
            if typed is None and "level" not in params and "level_percent" in params:
                # Чип Mini App (25.10): отступ от mark в сторону убытка — LONG
                # ниже, SHORT выше. В строку действия — уже цена (params ниже):
                # «Да» сверяет её, а не отступ от нового mark.
                percent = _parse_percent(params)
                if percent is None:
                    return _refuse(
                        Code.INVALID_PRICE,
                        "Отступ стопа — положительное число процентов меньше 100.",
                    )
                typed = inputs.mark * (ONE - _sign(p.side) * percent / HUNDRED)
            if typed is None:
                return _refuse(Code.INVALID_PRICE, "Цена стопа — положительное число.")
            # К шагу цены — ближе к цене: объём фиксирован, риск в $ не растёт.
            level = stop_closer(typed, p.side, step)
        if not stop_clear_of_mark(level, inputs.mark, inputs.min_distance_percent, p.side):
            if breakeven:
                # К4 (осмотр 27.10): порог прямо — «доступно, когда mark не ниже X», а не
                # «безубыток Z: mark ещё не ушёл за него на 0.20%». «Не ниже/не выше» (К9):
                # при mark = X стоп проходит — «выше X» читалось как строго.
                x = breakeven_threshold(p.side, level, inputs.min_distance_percent, precision)
                when = f"когда mark {'не ниже' if long else 'не выше'} {fmt_price(x, precision)}"
                return ExecutionRefusal(
                    Code.BREAKEVEN_NOT_REACHED,
                    f"Стоп в безубыток доступен, {when} (сейчас "
                    f"{fmt_price(inputs.mark, precision)}, безубыток "
                    f"{fmt_price(level, precision)}).",
                    hint=f"доступно, {when}",
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
        stored: dict[str, object] = {"breakeven": True} if breakeven else {"level": str(level)}
        if percent is not None:
            stored["level_percent"] = str(percent)
        return ActionPlan(
            kind=kind,
            params=stored,
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
        level = take_closer(typed_take, p.side, step)   # A.4: тейк — ближе
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


def _parse_percent(params: dict[str, object]) -> Decimal | None:
    try:
        percent = Decimal(str(params.get("level_percent")).strip().replace(",", "."))
    except (ArithmeticError, ValueError):
        return None
    if not percent.is_finite() or percent <= ZERO or percent >= HUNDRED:
        return None
    return percent


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
        # 5.0а (25.10): зафиксировано меньше цента (безубыток округлён вверх —
        # плюс в доли цента) — «≈0», а не «зафиксировано 0.00».
        locked = _cents(-loss)
        text = f"нет, зафиксировано {fmt_amount(locked)} USDT" if locked > ZERO else "≈0 USDT"
        if r is not None:
            r = -r   # 5.0б (25.10): здесь R — результат, не риск: прибыль «+», убыток «−»
    else:
        text = f"{fmt_amount(loss)} USDT"
    if r is None:
        return text
    if r.is_zero():
        return f"{text} ({r.copy_abs()}R)"   # «-0.00R» → «0.00R»: ноль без знака
    plus = "+" if loss <= ZERO and r > ZERO else ""   # величина риска — без плюса
    return f"{text} ({plus}{r}R)"


@dataclass(frozen=True, slots=True)
class RiskView:
    """Риск до и после строками карточки. increase — «Да, увеличить риск»:
    тогда без R в скобках, рост в R — delta («на 0.65R больше» — прирост, не абсолют, К4;
    R не знаем — "")."""

    before: str
    after: str
    increase: bool
    delta: str


@dataclass(frozen=True, slots=True)
class CardView:
    """Поля карточки действия строками (5.0, 25.10.2026): одно место, где числа
    плана форматируются (fmt_*). Из него — текст карточки в чате (render_card)
    и строки карточки Mini App (app/api) — числа совпадают по построению."""

    kind: Kind
    symbol: str
    side: str
    title: str
    quantity: str
    entry: str
    mark: str
    old_level: str | None = None      # стоп/тейк: что стоит сейчас, «нет» — ничего
    new_level: str | None = None
    close_what: str | None = None     # «всю позицию» / «25%»
    close_qty: str | None = None
    fee: str = ""                     # комиссия выхода, USDT
    pnl: str | None = None            # тейк — по тейку, закрытие — по mark
    remainder: str | None = None      # частичное: что будет со стопом и тейком
    risk: RiskView | None = None      # тейк — None
    breakeven: bool = False
    risk_share: str | None = None     # «0.01% от 10000.00 USDT»

    @property
    def fee_text(self) -> str:
        """Комиссия в строке карточки: «≈0.02»; меньше цента — «<0.01» без «≈» (К4)."""
        return self.fee if "<" in self.fee else f"≈{self.fee}"


def card_view(plan: ActionPlan, inputs: ActionInputs) -> CardView:
    p = inputs.position
    pp = inputs.symbol_info.price_precision
    qp = inputs.symbol_info.quantity_precision
    breakeven = plan.kind is Kind.MOVE_STOP and bool(plan.params.get("breakeven"))
    title = "стоп в безубыток" if breakeven else KIND_TITLE[plan.kind]
    old_level = new_level = close_what = close_qty = pnl = remainder = None
    if plan.kind in (Kind.MOVE_STOP, Kind.SET_TAKE):
        current = plan.current_stop if plan.kind is Kind.MOVE_STOP else plan.current_take
        old_level = fmt_price(current, pp) if current is not None else "нет"
        new_level = fmt_price(plan.new_level, pp)
        if plan.kind is Kind.SET_TAKE and plan.pnl_estimate is not None:
            pnl = fmt_money(plan.pnl_estimate)
    else:
        close_what = (
            "всю позицию" if plan.kind is Kind.CLOSE_FULL else f"{plan.params.get('fraction')}%"
        )
        close_qty = fmt_qty(plan.close_qty, qp)
        if plan.pnl_estimate is not None:
            pnl = fmt_money(plan.pnl_estimate)
        if plan.kind is Kind.CLOSE_PARTIAL and plan.remainder is not None:
            remainder = levels_after_partial(inputs.stops, inputs.takes, plan.remainder, qp)
    risk = None
    if plan.kind is not Kind.SET_TAKE:
        if plan.risk_increase:
            delta = (
                f"на {(plan.r_after - plan.r_before).quantize(R_STEP)}R больше"
                if plan.r_after is not None and plan.r_before is not None else ""
            )
            risk = RiskView(_risk_text(plan.risk_before, None),
                            _risk_text(plan.risk_after, None), True, delta)
        else:
            risk = RiskView(_risk_text(plan.risk_before, plan.r_before),
                            _risk_text(plan.risk_after, plan.r_after), False, "")
    share = None
    # 5.0а: «Риск после» — только если действие риск меняет (у тейка — нет).
    if (inputs.equity and plan.risk_after is not None and plan.risk_after > ZERO
            and plan.risk_after != plan.risk_before):
        # К4: доля меньше 0.01% — «<0.01%», не «0.00%» (риск есть, просто мал).
        pct = fmt_small_pct(plan.risk_after / inputs.equity * HUNDRED)
        share = f"{pct} от {fmt_amount(inputs.equity)} USDT"
    return CardView(
        kind=plan.kind, symbol=p.symbol, side=p.side.value, title=title,
        quantity=fmt_qty(p.quantity, qp), entry=fmt_price(p.entry_price, pp),
        mark=fmt_price(inputs.mark, pp), old_level=old_level, new_level=new_level,
        close_what=close_what, close_qty=close_qty, fee=fmt_fee(plan.fee), pnl=pnl,
        remainder=remainder, risk=risk, breakeven=breakeven, risk_share=share,
    )


def render_card(plan: ActionPlan, inputs: ActionInputs) -> str:
    v = card_view(plan, inputs)
    # Текст чата — HTML: «<0.01» в комиссии и «Риск после» экранируется (К4), иначе Telegram
    # читает «<» как тег и отклоняет сообщение. Строки Mini App (card_view) — без экранирования.
    fee = html.escape(v.fee_text, quote=False)
    lines = [
        f"<b>{v.symbol} {v.side} · {v.title}</b>",
        f"Позиция {v.quantity} · вход {v.entry} · mark {v.mark}",
    ]
    if v.kind in (Kind.MOVE_STOP, Kind.SET_TAKE):
        name = "Стоп" if v.kind is Kind.MOVE_STOP else "Тейк"
        lines.append(f"{name}: {v.old_level} → <b>{v.new_level}</b> (на всю позицию)")
        if v.pnl is not None:
            lines.append(f"PnL по тейку ≈ {v.pnl} USDT (после комиссий)")
    else:
        lines.append(
            f"Закрыть {v.close_what}: <b>{v.close_qty}</b> маркетом · комиссия {fee} USDT"
        )
        if v.pnl is not None:
            lines.append(f"PnL ≈ {v.pnl} USDT по mark (после комиссий)")
        if v.remainder is not None:
            lines.append(v.remainder)
    if v.risk is not None:
        if v.risk.increase:
            delta = f" ({v.risk.delta})" if v.risk.delta else ""
            lines.append(
                f"⚠️ <b>Риск увеличится:</b> {v.risk.before} → {v.risk.after}{delta}"
            )
        else:
            lines.append(f"Риск: {v.risk.before} → {v.risk.after}")
        if v.kind is Kind.MOVE_STOP:
            lines.append(f"Комиссия выхода по стопу {fee} USDT (учтена в риске)")
        if v.breakeven:
            lines.append("≈0 нетто без учёта проскальзывания (стоп исполняется маркетом)")
    if v.risk_share is not None:
        lines.append(f"Риск после — {html.escape(v.risk_share, quote=False)}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)
