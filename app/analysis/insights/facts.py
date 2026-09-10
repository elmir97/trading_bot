"""FactPack — детерминированный слепок торговли за период.

Единственный источник цифр для AI-слоя. LLM не получает сырые сделки и
не считает ничего сам: всё, что он видит, посчитано здесь, на Decimal.

Модуль чистый: никакого I/O, никакого SQLAlchemy. На вход — список
`TradeFact`, который собирает адаптер в `app/analysis/insights/loader.py`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Iterable, Sequence

from app.analysis.insights import rules

ZERO = Decimal("0")
MONEY = Decimal("0.01")
RATIO = Decimal("0.01")


def _q(value: Decimal | None, exp: Decimal = MONEY) -> Decimal | None:
    return None if value is None else value.quantize(exp)


@dataclass(frozen=True, slots=True)
class TradeFact:
    """Плоское представление закрытой сделки.

    Намеренно не привязано к ORM-модели: домен не должен знать про базу,
    а при изменении схемы правится только адаптер.
    """

    id: int
    symbol: str
    side: str
    strategy: str | None
    timeframe: str | None
    opened_at: datetime
    closed_at: datetime
    net_pnl: Decimal
    risk_amount: Decimal | None
    planned_rr: Decimal | None
    realized_r: Decimal | None
    mistakes: tuple[str, ...] = ()

    @property
    def is_win(self) -> bool:
        return self.net_pnl > ZERO

    @property
    def duration_hours(self) -> Decimal:
        seconds = Decimal((self.closed_at - self.opened_at).total_seconds())
        return seconds / Decimal("3600")


@dataclass(frozen=True, slots=True)
class GroupStat:
    """Агрегат по произвольному срезу сделок."""

    key: str
    trades: int
    wins: int
    net_pnl: Decimal
    win_rate: Decimal
    expectancy: Decimal
    expectancy_r: Decimal | None
    profit_factor: Decimal | None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": str(_q(self.win_rate, RATIO)),
            "net_pnl": str(_q(self.net_pnl)),
            "expectancy": str(_q(self.expectancy)),
            "expectancy_r": None if self.expectancy_r is None else str(_q(self.expectancy_r, RATIO)),
            "profit_factor": None if self.profit_factor is None else str(_q(self.profit_factor, RATIO)),
        }


def build_group(key: str, trades: Sequence[TradeFact]) -> GroupStat:
    """Считает агрегат по одной группе сделок."""
    count = len(trades)
    if count == 0:
        return GroupStat(key, 0, 0, ZERO, ZERO, ZERO, None, None)

    wins = sum(1 for t in trades if t.is_win)
    net = sum((t.net_pnl for t in trades), ZERO)
    gross_profit = sum((t.net_pnl for t in trades if t.net_pnl > ZERO), ZERO)
    gross_loss = -sum((t.net_pnl for t in trades if t.net_pnl < ZERO), ZERO)

    r_values = [t.realized_r for t in trades if t.realized_r is not None]
    expectancy_r = (sum(r_values, ZERO) / Decimal(len(r_values))) if r_values else None

    profit_factor = (gross_profit / gross_loss) if gross_loss > ZERO else None

    return GroupStat(
        key=key,
        trades=count,
        wins=wins,
        net_pnl=net,
        win_rate=Decimal(wins) / Decimal(count) * Decimal("100"),
        expectancy=net / Decimal(count),
        expectancy_r=expectancy_r,
        profit_factor=profit_factor,
    )


def group_by(
    trades: Sequence[TradeFact],
    key_fn: Callable[[TradeFact], str | None],
    *,
    min_sample: int = rules.MIN_GROUP_SAMPLE,
) -> list[GroupStat]:
    """Разбивает сделки по ключу и отбрасывает группы меньше порога.

    Отсев на этом уровне важен: группа из двух сделок не должна доезжать
    ни до отчёта, ни до модели — иначе она будет истолкована как тенденция.
    """
    buckets: dict[str, list[TradeFact]] = defaultdict(list)
    for trade in trades:
        key = key_fn(trade)
        if key is not None:
            buckets[key].append(trade)

    stats = [build_group(key, items) for key, items in buckets.items() if len(items) >= min_sample]
    return sorted(stats, key=lambda s: s.net_pnl)


def _duration_bucket(trade: TradeFact) -> str:
    hours = trade.duration_hours
    for name, low, high in rules.DURATION_BUCKETS:
        if hours >= low and (high is None or hours < high):
            return name
    return rules.DURATION_BUCKETS[-1][0]


def _hour_bucket(trade: TradeFact) -> str:
    hour = trade.opened_at.hour
    for name, low, high in rules.HOUR_BUCKETS:
        if low <= hour < high:
            return name
    return rules.HOUR_BUCKETS[0][0]


def _weekday(trade: TradeFact) -> str:
    return rules.WEEKDAY_NAMES[trade.opened_at.weekday()]


def max_drawdown(trades: Sequence[TradeFact]) -> Decimal:
    """Максимальная просадка по кривой накопленного PnL, в валюте счёта."""
    peak = ZERO
    equity = ZERO
    worst = ZERO
    for trade in sorted(trades, key=lambda t: t.closed_at):
        equity += trade.net_pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def longest_streak(trades: Sequence[TradeFact], *, wins: bool) -> int:
    """Самая длинная серия выигрышей или проигрышей подряд."""
    best = 0
    current = 0
    for trade in sorted(trades, key=lambda t: t.closed_at):
        if trade.is_win is wins:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


@dataclass(frozen=True, slots=True)
class FactPack:
    """Всё, что модель получит на вход, кроме findings."""

    period_start: datetime
    period_end: datetime
    overall: GroupStat
    max_drawdown: Decimal
    win_streak: int
    loss_streak: int
    by_symbol: list[GroupStat]
    by_strategy: list[GroupStat]
    by_side: list[GroupStat]
    by_timeframe: list[GroupStat]
    by_weekday: list[GroupStat]
    by_hour: list[GroupStat]
    by_duration: list[GroupStat]
    by_mistake: list[GroupStat]

    @property
    def is_sufficient(self) -> bool:
        return self.overall.trades >= rules.MIN_PERIOD_SAMPLE

    def to_dict(self) -> dict:
        return {
            "period": {
                "start": self.period_start.isoformat(),
                "end": self.period_end.isoformat(),
            },
            "overall": self.overall.to_dict(),
            "max_drawdown": str(_q(self.max_drawdown)),
            "win_streak": self.win_streak,
            "loss_streak": self.loss_streak,
            "by_symbol": [s.to_dict() for s in self.by_symbol],
            "by_strategy": [s.to_dict() for s in self.by_strategy],
            "by_side": [s.to_dict() for s in self.by_side],
            "by_timeframe": [s.to_dict() for s in self.by_timeframe],
            "by_weekday": [s.to_dict() for s in self.by_weekday],
            "by_hour": [s.to_dict() for s in self.by_hour],
            "by_duration": [s.to_dict() for s in self.by_duration],
            "by_mistake": [s.to_dict() for s in self.by_mistake],
        }

    def fingerprint(self) -> str:
        """Стабильный хеш содержимого — ключ кэша отчётов.

        Один и тот же набор фактов не должен уходить в API дважды.
        """
        payload = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_fact_pack(
    trades: Sequence[TradeFact],
    period_start: datetime,
    period_end: datetime,
) -> FactPack:
    """Собирает все срезы за период."""

    def mistake_groups() -> list[GroupStat]:
        buckets: dict[str, list[TradeFact]] = defaultdict(list)
        for trade in trades:
            for code in trade.mistakes:
                buckets[code].append(trade)
        stats = [
            build_group(code, items)
            for code, items in buckets.items()
            if len(items) >= rules.MIN_GROUP_SAMPLE
        ]
        return sorted(stats, key=lambda s: s.net_pnl)

    return FactPack(
        period_start=period_start,
        period_end=period_end,
        overall=build_group("overall", trades),
        max_drawdown=max_drawdown(trades),
        win_streak=longest_streak(trades, wins=True),
        loss_streak=longest_streak(trades, wins=False),
        by_symbol=group_by(trades, lambda t: t.symbol),
        by_strategy=group_by(trades, lambda t: t.strategy),
        by_side=group_by(trades, lambda t: t.side),
        by_timeframe=group_by(trades, lambda t: t.timeframe),
        by_weekday=group_by(trades, _weekday),
        by_hour=group_by(trades, _hour_bucket),
        by_duration=group_by(trades, _duration_bucket),
        by_mistake=mistake_groups(),
    )
