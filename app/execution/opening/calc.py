"""Расчёт карточки открытия — чистые функции, без I/O (§1, §5 плана).

Объём от риска с комиссией (решение владельца 05.10): при срабатывании стопа
убыток = объём × (дистанция + комиссия входа и выхода по тейкеру), и он не
больше equity × риск%. Объём — вниз под шаг лота. Ликвидация до входа —
оценка для изолированной маржи с консервативной поддерживающей маржой;
после входа ядро сверяет фактическую liquidationPrice.

Технические отказы (стоп не с той стороны, объём меньше минимума, маржи не
хватает, ликвидация ближе запаса, позиция по стороне уже есть, режим маржи
не сменить) — здесь; нарушения торгового плана — app/execution/opening/checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.core.numfmt import fmt_amount, fmt_pct, fmt_price, fmt_qty
from app.exchanges.base import MarginType, Position, SymbolInfo
from app.trading.enums import EntryType, TradeSide

ZERO = Decimal(0)
HUNDRED = Decimal(100)
CENT = Decimal("0.01")
_MARGIN_LABEL = {MarginType.ISOLATED: "изолированная", MarginType.CROSSED: "кросс"}


class Level(StrEnum):
    BLOCK = "BLOCK"   # кнопки «Открыть» нет
    WARN = "WARN"     # «Открыть всё равно»


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    level: Level
    message: str

    def as_json(self) -> dict[str, str]:
        return {"code": self.code, "level": self.level.value, "message": self.message}


@dataclass(frozen=True, slots=True)
class OpeningInputs:
    """Ввод пользователя (мастер или Mini App)."""

    symbol: str
    side: TradeSide
    entry_type: EntryType
    stop_loss: Decimal
    risk_percent: Decimal
    leverage: int
    limit_price: Decimal | None = None
    take_profit: Decimal | None = None
    expiry_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Данные биржи на момент карточки."""

    last_price: Decimal
    symbol_info: SymbolInfo
    equity: Decimal
    available: Decimal | None
    taker_rate: Decimal
    max_leverage: int              # максимум биржи по стороне
    margin_type: MarginType        # режим символа на бирже сейчас
    desired_margin_type: MarginType
    position: Position | None      # позиция по этой стороне
    symbol_busy: bool              # по символу есть позиции (любая сторона) или ордера


@dataclass(frozen=True, slots=True)
class Limits:
    plan_risk_percent: Decimal | None   # максимум риска на сделку по плану
    plan_max_leverage: int | None
    min_stop_distance_percent: Decimal
    liq_buffer: Decimal
    mmr: Decimal


@dataclass(frozen=True, slots=True)
class OpeningCalc:
    entry_price: Decimal            # рынок — last, лимит — цена лимита
    distance: Decimal | None        # до стопа, > 0 при верной стороне
    distance_percent: Decimal | None
    quantity: Decimal               # 0 — не посчитан (технический блок)
    notional: Decimal
    risk_usd: Decimal               # с комиссией входа и выхода по стопу
    risk_percent: Decimal           # risk_usd от equity
    fee_usd: Decimal                # комиссия входа и выхода по стопу
    margin: Decimal
    liq_estimate: Decimal | None    # только изолированная
    liq_ratio: Decimal | None       # |вход − ликв.| / дистанция
    max_leverage_for_stop: int      # максимум плеча при этом стопе (запас ликвидации)
    suggested_leverage: int
    rr: Decimal | None
    fills_immediately: bool         # лимит пересекает рынок — исполнится сразу
    issues: tuple[Issue, ...]

    @property
    def blocked(self) -> bool:
        return any(i.level is Level.BLOCK for i in self.issues)


def lot_step(info: SymbolInfo) -> Decimal:
    return Decimal(1).scaleb(-info.quantity_precision)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Вниз до кратного шагу (объём от риска — только вниз)."""
    if value <= ZERO:
        return ZERO
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def liquidation_estimate(
    entry: Decimal, side: TradeSide, leverage: int, mmr: Decimal
) -> Decimal:
    """Изолированная маржа: LONG E·(1 − 1/L + mmr), SHORT E·(1 + 1/L − mmr)."""
    inv = Decimal(1) / Decimal(leverage)
    if side is TradeSide.LONG:
        return entry * (Decimal(1) - inv + mmr)
    return entry * (Decimal(1) + inv - mmr)


def max_leverage_for_stop(distance_fraction: Decimal, buffer: Decimal, mmr: Decimal) -> int:
    """Наибольшее целое L, при котором |вход − ликв.| ≥ buffer·|вход − стоп|:
    1/L − mmr ≥ buffer·d → L ≤ 1 / (buffer·d + mmr)."""
    denom = buffer * distance_fraction + mmr
    if denom <= ZERO:
        return 1
    return max(int((Decimal(1) / denom).to_integral_value(rounding=ROUND_DOWN)), 1)


def _pct(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _block(code: str, message: str) -> Issue:
    return Issue(code, Level.BLOCK, message)


def compute(inputs: OpeningInputs, market: MarketSnapshot, limits: Limits) -> OpeningCalc:
    side = inputs.side
    sign = Decimal(side.direction)
    info = market.symbol_info
    issues: list[Issue] = []
    entry = (
        inputs.limit_price
        if inputs.entry_type is EntryType.LIMIT and inputs.limit_price is not None
        else market.last_price
    )
    last = market.last_price
    below_above = "ниже" if side is TradeSide.LONG else "выше"

    # --- позиция и режим маржи ----------------------------------------------
    if market.position is not None:
        p = market.position
        issues.append(_block(
            "POSITION_EXISTS",
            f"По {inputs.symbol} уже открыт {side.value} "
            f"{fmt_qty(p.quantity, info.quantity_precision)} "
            f"(вход {fmt_price(p.entry_price, info.price_precision)}). Открытие из бота — только"
            " когда по этой стороне нет позиции: добавь к ней в «Позициях» или закрой.",
        ))
    if market.margin_type is not market.desired_margin_type and market.symbol_busy:
        issues.append(_block(
            "MARGIN_MODE_LOCKED",
            f"Режим маржи {inputs.symbol} на бирже — {_MARGIN_LABEL[market.margin_type]}, "
            f"для новых сделок выбран {_MARGIN_LABEL[market.desired_margin_type]}. Сменить его "
            "нельзя, пока по символу есть позиция или ордера.",
        ))

    # --- ввод ----------------------------------------------------------------
    if inputs.entry_type is EntryType.LIMIT and (
        inputs.limit_price is None or inputs.limit_price <= ZERO
    ):
        issues.append(_block("INVALID_PRICE", "Цена лимита должна быть больше нуля."))
    if inputs.risk_percent <= ZERO:
        issues.append(_block("INVALID_RISK", "Риск должен быть больше нуля."))
    elif limits.plan_risk_percent is not None and _pct(inputs.risk_percent) > _pct(
        limits.plan_risk_percent
    ):
        issues.append(_block(
            "RISK_TOO_HIGH",
            f"Риск {fmt_pct(inputs.risk_percent)} выше максимума плана "
            f"{fmt_pct(limits.plan_risk_percent)}.",
        ))
    if inputs.leverage < 1:
        issues.append(_block("INVALID_LEVERAGE", "Плечо — целое число от 1."))
    elif inputs.leverage > market.max_leverage:
        issues.append(_block(
            "LEVERAGE_ABOVE_EXCHANGE",
            f"Плечо {inputs.leverage}x выше максимума биржи {market.max_leverage}x "
            f"для {inputs.symbol} {side.value}.",
        ))
    if market.equity <= ZERO:
        issues.append(_block(
            "NON_POSITIVE_EQUITY",
            f"Equity счёта {fmt_amount(market.equity)} — объём от риска не посчитать.",
        ))

    # --- стоп и тейк -------------------------------------------------------
    distance: Decimal | None = (entry - inputs.stop_loss) * sign
    if distance is not None and distance <= ZERO:
        issues.append(_block(
            "STOP_WRONG_SIDE",
            f"Стоп {side.value} должен быть {below_above} цены входа "
            f"{fmt_price(entry, info.price_precision)}.",
        ))
        distance = None
    elif (last - inputs.stop_loss) * sign <= ZERO:
        # Р4: биржа сверяет стоп с last price и отклоняет весь вход.
        issues.append(_block(
            "STOP_WRONG_SIDE",
            f"Стоп {side.value} должен быть {below_above} текущей цены "
            f"{fmt_price(last, info.price_precision)} — иначе он сработал бы сразу, и биржа "
            "отклонит вход.",
        ))
    distance_percent = distance / entry * HUNDRED if distance is not None else None
    if distance_percent is not None and distance_percent < limits.min_stop_distance_percent:
        issues.append(_block(
            "STOP_TOO_CLOSE",
            f"Стоп в {fmt_pct(distance_percent)} от входа — ближе минимума "
            f"{fmt_pct(limits.min_stop_distance_percent)}.",
        ))
    rr: Decimal | None = None
    if inputs.take_profit is not None:
        reward = (inputs.take_profit - entry) * sign
        if reward <= ZERO or (inputs.take_profit - last) * sign <= ZERO:
            above = "выше" if side is TradeSide.LONG else "ниже"
            issues.append(_block(
                "TAKE_WRONG_SIDE",
                f"Тейк {side.value} должен быть {above} цены входа и текущей цены.",
            ))
        elif distance is not None:
            rr = reward / distance

    fills_immediately = (
        inputs.entry_type is EntryType.LIMIT
        and inputs.limit_price is not None
        and (inputs.limit_price - last) * sign >= ZERO
    )

    # --- объём, риск, маржа --------------------------------------------------
    quantity = notional = risk_usd = risk_percent = fee_usd = margin = ZERO
    fee_per_unit = (entry + inputs.stop_loss) * market.taker_rate
    if distance is not None and market.equity > ZERO and inputs.risk_percent > ZERO:
        budget = market.equity * inputs.risk_percent / HUNDRED
        quantity = floor_to_step(budget / (distance + fee_per_unit), lot_step(info))
        notional = quantity * entry
        risk_usd = quantity * (distance + fee_per_unit)
        fee_usd = quantity * fee_per_unit
        risk_percent = risk_usd / market.equity * HUNDRED
        if inputs.leverage >= 1:
            margin = notional / Decimal(inputs.leverage)
        if quantity <= ZERO or quantity < info.min_quantity or notional < info.min_notional:
            issues.append(_block(
                "SIZE_TOO_SMALL",
                f"Объём {fmt_qty(quantity, info.quantity_precision)} меньше минимума биржи "
                f"({fmt_qty(info.min_quantity, info.quantity_precision)} "
                f"{inputs.symbol.split('-')[0]} / {fmt_amount(info.min_notional)} USDT) — "
                "увеличь риск или сократи стоп.",
            ))
        elif inputs.leverage >= 1:
            need = margin + notional * market.taker_rate
            if market.available is None:
                issues.append(_block(
                    "AVAILABLE_MARGIN_UNKNOWN",
                    "Биржа не отдала свободную маржу — проверить нечем.",
                ))
            elif need > market.available:
                issues.append(_block(
                    "INSUFFICIENT_MARGIN",
                    f"Нужно маржи {fmt_amount(need)} USDT с комиссией входа, свободно "
                    f"{fmt_amount(market.available)}.",
                ))

    # --- ликвидация ----------------------------------------------------------
    liq: Decimal | None = None
    liq_ratio: Decimal | None = None
    max_for_stop = market.max_leverage
    if distance is not None:
        max_for_stop = max_leverage_for_stop(distance / entry, limits.liq_buffer, limits.mmr)
        if market.desired_margin_type is MarginType.ISOLATED and inputs.leverage >= 1:
            liq = liquidation_estimate(entry, side, inputs.leverage, limits.mmr)
            liq_ratio = (entry - liq) * sign / distance
            if liq_ratio < limits.liq_buffer:
                cap = min(
                    max_for_stop, market.max_leverage,
                    limits.plan_max_leverage or market.max_leverage,
                )
                issues.append(_block(
                    "LIQ_TOO_CLOSE",
                    f"При стопе −{fmt_pct(distance_percent or ZERO)} и плече {inputs.leverage}x "
                    f"ликвидация ≈ {fmt_price(liq, info.price_precision)} слишком близко "
                    f"(запас {liq_ratio.quantize(Decimal('0.1'), ROUND_DOWN)}× при нужных "
                    f"{limits.liq_buffer}×): максимум {cap}x.",
                ))
    suggested = min(
        max_for_stop, market.max_leverage, limits.plan_max_leverage or market.max_leverage
    )

    issues.sort(key=lambda i: 0 if i.level is Level.BLOCK else 1)
    return OpeningCalc(
        entry_price=entry, distance=distance, distance_percent=distance_percent,
        quantity=quantity, notional=notional, risk_usd=risk_usd, risk_percent=risk_percent,
        fee_usd=fee_usd, margin=margin, liq_estimate=liq, liq_ratio=liq_ratio,
        max_leverage_for_stop=max_for_stop, suggested_leverage=max(suggested, 1), rr=rr,
        fills_immediately=fills_immediately, issues=tuple(issues),
    )
