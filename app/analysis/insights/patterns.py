"""Детекторы закономерностей поверх FactPack.

Каждый детектор возвращает структурированный `Finding` с кодом, размером
выборки и посчитанным эффектом в деньгах и в R. Модель потом объясняет
findings словами, но не порождает новые и не меняет цифры.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Sequence

from app.analysis.insights import rules
from app.analysis.insights.facts import (
    ZERO,
    FactPack,
    GroupStat,
    TradeFact,
    build_group,
)


@dataclass(frozen=True, slots=True)
class Finding:
    """Одна найденная закономерность."""

    code: str
    scope: str
    title: str
    sample_size: int
    effect_pnl: Decimal
    effect_r: Decimal | None
    confidence: str
    details: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "scope": self.scope,
            "title": self.title,
            "sample_size": self.sample_size,
            "effect_pnl": str(self.effect_pnl.quantize(Decimal("0.01"))),
            "effect_r": None if self.effect_r is None else str(self.effect_r.quantize(Decimal("0.01"))),
            "confidence": self.confidence,
            "details": self.details,
        }


def _gap_findings(
    pack: FactPack,
    groups: Sequence[GroupStat],
    scope: str,
    label: str,
) -> list[Finding]:
    """Группы, чья expectancy в R заметно отличается от общей."""
    baseline = pack.overall.expectancy_r
    if baseline is None:
        return []

    findings: list[Finding] = []
    for group in groups:
        if group.expectancy_r is None:
            continue
        gap = group.expectancy_r - baseline
        if abs(gap) < rules.MIN_EXPECTANCY_GAP_R:
            continue
        direction = "WEAK" if gap < ZERO else "STRONG"
        findings.append(
            Finding(
                code=f"{scope}_{direction}",
                scope=scope,
                title=f"{label}: {group.key}",
                sample_size=group.trades,
                effect_pnl=group.net_pnl,
                effect_r=gap,
                confidence=rules.confidence_for(group.trades),
                details={
                    "group": group.key,
                    "win_rate": str(group.win_rate.quantize(Decimal("0.01"))),
                    "expectancy_r": str(group.expectancy_r.quantize(Decimal("0.01"))),
                    "baseline_r": str(baseline.quantize(Decimal("0.01"))),
                },
            )
        )
    return findings


def detect_early_exit(trades: Sequence[TradeFact]) -> list[Finding]:
    """Прибыльные сделки, закрытые заметно раньше планового RR.

    Эффект считается как недополученная прибыль при доходе до цели —
    оценка сверху, о чём модели сообщается явно в промпте.
    """
    affected = [
        t
        for t in trades
        if t.is_win
        and t.planned_rr is not None
        and t.realized_r is not None
        and t.planned_rr > ZERO
        and t.realized_r < t.planned_rr * rules.EARLY_EXIT_RATIO
    ]
    if len(affected) < rules.MIN_GROUP_SAMPLE:
        return []

    missed = ZERO
    for trade in affected:
        if trade.risk_amount:
            missed += (trade.planned_rr - trade.realized_r) * trade.risk_amount

    return [
        Finding(
            code="EARLY_EXIT",
            scope="behaviour",
            title="Ранняя фиксация прибыли",
            sample_size=len(affected),
            effect_pnl=missed,
            effect_r=None,
            confidence=rules.confidence_for(len(affected)),
            details={
                "share_of_wins": str(len(affected)),
                "note": "оценка недополученной прибыли при доходе до планового TP",
            },
        )
    ]


def detect_revenge_trades(trades: Sequence[TradeFact]) -> list[Finding]:
    """Входы вскоре после закрытия убытка."""
    ordered = sorted(trades, key=lambda t: t.opened_at)
    window = timedelta(minutes=rules.REVENGE_WINDOW_MINUTES)
    losses = sorted((t.closed_at for t in trades if not t.is_win))

    suspects: list[TradeFact] = []
    for trade in ordered:
        if any(closed <= trade.opened_at <= closed + window for closed in losses):
            suspects.append(trade)

    if len(suspects) < rules.MIN_GROUP_SAMPLE:
        return []

    stat = build_group("revenge", suspects)
    rest = [t for t in trades if t not in suspects]
    baseline = build_group("rest", rest)
    gap = None
    if stat.expectancy_r is not None and baseline.expectancy_r is not None:
        gap = stat.expectancy_r - baseline.expectancy_r

    return [
        Finding(
            code="REVENGE_TRADE",
            scope="behaviour",
            title=f"Вход в течение {rules.REVENGE_WINDOW_MINUTES} мин после убытка",
            sample_size=stat.trades,
            effect_pnl=stat.net_pnl,
            effect_r=gap,
            confidence=rules.confidence_for(stat.trades),
            details={
                "win_rate": str(stat.win_rate.quantize(Decimal("0.01"))),
                "baseline_win_rate": str(baseline.win_rate.quantize(Decimal("0.01"))),
            },
        )
    ]


def detect_overtrading(trades: Sequence[TradeFact]) -> list[Finding]:
    """Дни с перегруженным количеством входов."""
    by_day: dict[str, list[TradeFact]] = defaultdict(list)
    for trade in trades:
        by_day[trade.opened_at.date().isoformat()].append(trade)

    heavy = [t for items in by_day.values() if len(items) >= rules.OVERTRADING_DAILY_TRADES for t in items]
    normal = [t for items in by_day.values() if len(items) < rules.OVERTRADING_DAILY_TRADES for t in items]
    if len(heavy) < rules.MIN_GROUP_SAMPLE or not normal:
        return []

    heavy_stat = build_group("heavy", heavy)
    normal_stat = build_group("normal", normal)
    gap = None
    if heavy_stat.expectancy_r is not None and normal_stat.expectancy_r is not None:
        gap = heavy_stat.expectancy_r - normal_stat.expectancy_r

    return [
        Finding(
            code="OVERTRADING",
            scope="behaviour",
            title=f"Дни с {rules.OVERTRADING_DAILY_TRADES}+ входами",
            sample_size=heavy_stat.trades,
            effect_pnl=heavy_stat.net_pnl,
            effect_r=gap,
            confidence=rules.confidence_for(heavy_stat.trades),
            details={
                "heavy_days": str(sum(1 for i in by_day.values() if len(i) >= rules.OVERTRADING_DAILY_TRADES)),
                "expectancy_normal": str((normal_stat.expectancy_r or ZERO).quantize(Decimal("0.01"))),
            },
        )
    ]


def detect_after_loss_streak(trades: Sequence[TradeFact]) -> list[Finding]:
    """Поведение после серии убытков подряд."""
    ordered = sorted(trades, key=lambda t: t.closed_at)
    after: list[TradeFact] = []
    streak = 0
    for trade in ordered:
        if streak >= rules.LOSS_STREAK_LENGTH:
            after.append(trade)
        streak = streak + 1 if not trade.is_win else 0

    if len(after) < rules.MIN_GROUP_SAMPLE:
        return []

    stat = build_group("after_loss_streak", after)
    return [
        Finding(
            code="AFTER_LOSS_STREAK",
            scope="behaviour",
            title=f"Сделки после {rules.LOSS_STREAK_LENGTH}+ убытков подряд",
            sample_size=stat.trades,
            effect_pnl=stat.net_pnl,
            effect_r=stat.expectancy_r,
            confidence=rules.confidence_for(stat.trades),
            details={"win_rate": str(stat.win_rate.quantize(Decimal("0.01")))},
        )
    ]


def detect_mistake_cost(pack: FactPack) -> list[Finding]:
    """Финансовая цена размеченных ошибок."""
    findings: list[Finding] = []
    for group in pack.by_mistake:
        if group.net_pnl >= ZERO:
            continue
        findings.append(
            Finding(
                code="MISTAKE_COST",
                scope="mistakes",
                title=f"Цена ошибки: {group.key}",
                sample_size=group.trades,
                effect_pnl=group.net_pnl,
                effect_r=group.expectancy_r,
                confidence=rules.confidence_for(group.trades),
                details={"mistake_code": group.key},
            )
        )
    return findings


def detect_all(pack: FactPack, trades: Sequence[TradeFact]) -> list[Finding]:
    """Полный прогон детекторов. Порядок — по абсолютному эффекту в деньгах."""
    findings: list[Finding] = []
    findings += _gap_findings(pack, pack.by_symbol, "symbol", "Инструмент")
    findings += _gap_findings(pack, pack.by_strategy, "strategy", "Стратегия")
    findings += _gap_findings(pack, pack.by_side, "side", "Направление")
    findings += _gap_findings(pack, pack.by_timeframe, "timeframe", "Таймфрейм")
    findings += _gap_findings(pack, pack.by_weekday, "weekday", "День недели")
    findings += _gap_findings(pack, pack.by_hour, "hour", "Время входа")
    findings += _gap_findings(pack, pack.by_duration, "duration", "Время удержания")
    findings += detect_early_exit(trades)
    findings += detect_revenge_trades(trades)
    findings += detect_overtrading(trades)
    findings += detect_after_loss_streak(trades)
    findings += detect_mistake_cost(pack)

    return sorted(findings, key=lambda f: abs(f.effect_pnl), reverse=True)
