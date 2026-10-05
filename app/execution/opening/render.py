"""Тексты открытия для чата (карточка подтверждения, отказ).

Mini App рисует ту же карточку сам по JSON — числа одни и те же (calc)."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from app.core.numfmt import fmt_amount, fmt_pct, fmt_price, fmt_qty
from app.exchanges.base import MarginType
from app.execution.opening.calc import Issue, Level, OpeningCalc, OpeningInputs
from app.trading.enums import EntryType, ExchangeKeyMode

HUNDRED = Decimal(100)
_MARGIN = {MarginType.ISOLATED: "изолированная", MarginType.CROSSED: "кросс"}


def expiry_label(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60} ч"
    return f"{minutes} мин"


def base_asset(symbol: str) -> str:
    return symbol.split("-")[0]


def issues_block(issues: tuple[Issue, ...]) -> list[str]:
    return [
        f"{'🚫' if i.level is Level.BLOCK else '⚠️'} {i.message}" for i in issues
    ]


def render_card(
    inputs: OpeningInputs,
    calc: OpeningCalc,
    *,
    account_mode: ExchangeKeyMode,
    equity: Decimal,
    available: Decimal | None,
    margin_type: MarginType,
    price_precision: int,
    quantity_precision: int,
    dry_run: bool,
    ttl_seconds: int,
) -> str:
    p = price_precision
    side = inputs.side
    sign = Decimal(side.direction)
    head = (
        "⚠️ Реальный счёт — LIVE" if account_mode is ExchangeKeyMode.LIVE
        else "🟢 Открыть на бирже — DEMO"
    )
    if inputs.entry_type is EntryType.MARKET:
        entry_line = f"{inputs.symbol} {side.value} · Рынок (≈{fmt_price(calc.entry_price, p)})"
    else:
        entry_line = f"{inputs.symbol} {side.value} · Лимит {fmt_price(calc.entry_price, p)}"
        if inputs.expiry_minutes:
            entry_line += f" · срок {expiry_label(inputs.expiry_minutes)}"
    levels = f"Стоп {fmt_price(inputs.stop_loss, p)}"
    if calc.distance_percent is not None:
        levels += f" (−{fmt_pct(calc.distance_percent)})"
    if inputs.take_profit is not None:
        take_pct = (inputs.take_profit - calc.entry_price) * sign / calc.entry_price * HUNDRED
        levels += f" · Тейк {fmt_price(inputs.take_profit, p)} (+{fmt_pct(take_pct)})"
        if calc.rr is not None:
            levels += f" · RR {calc.rr.quantize(Decimal('0.01'))}"
    else:
        levels += " · без тейка"
    lines = [head, entry_line, levels]
    if calc.fills_immediately:
        lines.append("ℹ️ Лимит не хуже текущей цены — исполнится сразу, как рыночный.")
    if calc.quantity > 0:
        lines += [
            f"Риск: {fmt_amount(calc.risk_usd)} $ ({fmt_pct(calc.risk_percent)} от "
            f"{fmt_amount(equity)} $ equity), с комиссией",
            f"Комиссия ≈ {fmt_amount(calc.fee_usd)} $ (вход и выход по стопу)",
            f"Объём: {fmt_qty(calc.quantity, quantity_precision)} {base_asset(inputs.symbol)} "
            f"(≈{fmt_amount(calc.notional)} $)",
            f"Маржа: {fmt_amount(calc.margin)} $ ({_MARGIN[margin_type]}, плечо "
            f"{inputs.leverage}x) · свободно {fmt_amount(available)} $",
        ]
        if calc.liq_estimate is not None and calc.liq_ratio is not None:
            ratio = calc.liq_ratio.quantize(Decimal("0.1"), ROUND_DOWN)
            lines.append(
                f"Ликвидация ≈ {fmt_price(calc.liq_estimate, p)} (в {ratio} раза дальше стопа)"
            )
        elif margin_type is MarginType.CROSSED:
            lines.append("Ликвидация — по всему счёту (кросс), проверится после входа")
    if calc.issues:
        lines += ["", *issues_block(calc.issues)]
    if not calc.blocked:
        lines.append("")
        if dry_run:
            lines.append("🧪 Сухой прогон: на биржу ничего не уйдёт.")
        lines.append(f"Карточка действует {ttl_seconds} с.")
    return "\n".join(lines)


def render_refusal(message: str) -> str:
    return f"⛔ {message}"
