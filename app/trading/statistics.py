"""Расчёт торговой статистики.

Чистые функции над последовательностью закрытых сделок. Не знают про БД:
на вход — лёгкие снимки TradeSnapshot, на выход — датаклассы с числами.
Благодаря этому все метрики тестируются на заранее известных наборах,
включая вырожденные случаи, которые в реальной базе воспроизвести трудно.

Определения, где они расходятся между источниками, зафиксированы явно —
см. комментарии у Profit Factor, Expectancy и Max Drawdown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

ZERO = Decimal(0)
MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    """Минимум данных о закрытой сделке, нужный статистике.

    Отдельно от модели БД: запрос тянет только эти поля, а не сделку
    целиком со всеми исполнениями и связями.
    """

    pnl: Decimal
    closed_at: datetime
    symbol: str = ""
    side: str = ""
    strategy: str | None = None
    timeframe: str | None = None
    risk_reward: Decimal | None = None
    account_balance: Decimal | None = None

    @property
    def is_win(self) -> bool:
        return self.pnl > ZERO

    @property
    def is_loss(self) -> bool:
        return self.pnl < ZERO


@dataclass(frozen=True, slots=True)
class DrawdownResult:
    max_drawdown: Decimal          # в валюте депозита, положительное число
    max_drawdown_percent: Decimal  # от пика эквити
    peak_equity: Decimal
    trough_equity: Decimal
    current_drawdown: Decimal


@dataclass(frozen=True, slots=True)
class Statistics:
    total_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: Decimal
    total_pnl: Decimal
    average_pnl: Decimal
    average_win: Decimal
    average_loss: Decimal          # положительное число (размер убытка)
    largest_win: Decimal
    largest_loss: Decimal
    profit_factor: Decimal | None  # None = убытков не было
    expectancy: Decimal
    average_rr: Decimal | None
    max_win_streak: int
    max_loss_streak: int
    drawdown: DrawdownResult

    @property
    def loss_rate(self) -> Decimal:
        if self.total_trades == 0:
            return ZERO
        return (
            Decimal(self.losses) / Decimal(self.total_trades) * Decimal(100)
        ).quantize(PERCENT)


@dataclass(frozen=True, slots=True)
class GroupStats:
    """Срез статистики по одному значению группировки."""

    key: str
    trades: int
    wins: int
    win_rate: Decimal
    total_pnl: Decimal
    average_pnl: Decimal


def _safe_divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == ZERO:
        return ZERO
    return numerator / denominator


def calculate_drawdown(
    trades: Sequence[TradeSnapshot], starting_equity: Decimal = ZERO
) -> DrawdownResult:
    """Максимальная просадка по кривой эквити закрытых сделок.

    Считается peak-to-trough в порядке закрытия сделок, а не по дням:
    дневная просадка — отдельная метрика для лимитов торгового плана, и
    смешивать их нельзя. Здесь измеряется, насколько глубоко проседал
    результат от достигнутого максимума.

    Процент берётся от пика эквити, а не от начального депозита: просадка
    30% после удвоения счёта и просадка 30% от старта — разные события,
    и вторая гораздо опаснее.
    """
    ordered = sorted(trades, key=lambda t: t.closed_at)

    equity = starting_equity
    peak = starting_equity
    max_dd = ZERO
    max_dd_percent = ZERO
    peak_at_max = starting_equity
    trough_at_max = starting_equity

    for trade in ordered:
        equity += trade.pnl
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
            peak_at_max = peak
            trough_at_max = equity
            # Знаменатель — абсолютное значение пика: при отрицательном
            # пике (счёт ушёл ниже старта) процент иначе меняет знак.
            max_dd_percent = (
                _safe_divide(drawdown, abs(peak)) * Decimal(100)
                if peak != ZERO
                else ZERO
            )

    return DrawdownResult(
        max_drawdown=max_dd.quantize(MONEY),
        max_drawdown_percent=max_dd_percent.quantize(PERCENT),
        peak_equity=peak_at_max.quantize(MONEY),
        trough_equity=trough_at_max.quantize(MONEY),
        current_drawdown=(peak - equity).quantize(MONEY),
    )


def calculate_streaks(trades: Sequence[TradeSnapshot]) -> tuple[int, int]:
    """Самые длинные серии побед и убытков.

    Безубыточные сделки серию не продолжают и не обрывают — они просто
    не считаются ни победой, ни убытком.
    """
    ordered = sorted(trades, key=lambda t: t.closed_at)

    max_wins = max_losses = 0
    current_wins = current_losses = 0

    for trade in ordered:
        if trade.is_win:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        elif trade.is_loss:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)

    return max_wins, max_losses


def calculate_statistics(
    trades: Sequence[TradeSnapshot], starting_equity: Decimal = ZERO
) -> Statistics:
    """Полный набор метрик по набору закрытых сделок."""
    total = len(trades)

    if total == 0:
        return Statistics(
            total_trades=0, wins=0, losses=0, breakeven=0,
            win_rate=ZERO, total_pnl=ZERO, average_pnl=ZERO,
            average_win=ZERO, average_loss=ZERO,
            largest_win=ZERO, largest_loss=ZERO,
            profit_factor=None, expectancy=ZERO, average_rr=None,
            max_win_streak=0, max_loss_streak=0,
            drawdown=calculate_drawdown([], starting_equity),
        )

    wins = [t for t in trades if t.is_win]
    losses = [t for t in trades if t.is_loss]
    breakeven = total - len(wins) - len(losses)

    gross_profit = sum((t.pnl for t in wins), ZERO)
    gross_loss = sum((-t.pnl for t in losses), ZERO)  # положительное число
    total_pnl = sum((t.pnl for t in trades), ZERO)

    average_win = _safe_divide(gross_profit, Decimal(len(wins))) if wins else ZERO
    average_loss = _safe_divide(gross_loss, Decimal(len(losses))) if losses else ZERO

    # Profit Factor = валовая прибыль / валовой убыток.
    # При нулевых убытках возвращаем None, а не бесконечность: «бесконечный»
    # profit factor на трёх сделках вводит в заблуждение сильнее, чем пропуск.
    profit_factor = (
        (gross_profit / gross_loss).quantize(Decimal("0.001"))
        if gross_loss > ZERO
        else None
    )

    win_rate = _safe_divide(Decimal(len(wins)), Decimal(total)) * Decimal(100)

    # Expectancy — математическое ожидание на сделку в валюте депозита.
    # Эквивалентно среднему PnL, но записано через win rate, чтобы было
    # видно, из чего оно складывается.
    loss_rate = _safe_divide(Decimal(len(losses)), Decimal(total))
    expectancy = (win_rate / Decimal(100)) * average_win - loss_rate * average_loss

    rr_values = [t.risk_reward for t in trades if t.risk_reward is not None]
    average_rr = (
        (sum(rr_values, ZERO) / Decimal(len(rr_values))).quantize(Decimal("0.01"))
        if rr_values
        else None
    )

    max_win_streak, max_loss_streak = calculate_streaks(trades)

    return Statistics(
        total_trades=total,
        wins=len(wins),
        losses=len(losses),
        breakeven=breakeven,
        win_rate=win_rate.quantize(PERCENT),
        total_pnl=total_pnl.quantize(MONEY),
        average_pnl=_safe_divide(total_pnl, Decimal(total)).quantize(MONEY),
        average_win=average_win.quantize(MONEY),
        average_loss=average_loss.quantize(MONEY),
        largest_win=max((t.pnl for t in wins), default=ZERO).quantize(MONEY),
        largest_loss=min((t.pnl for t in losses), default=ZERO).quantize(MONEY),
        profit_factor=profit_factor,
        expectancy=expectancy.quantize(MONEY),
        average_rr=average_rr,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        drawdown=calculate_drawdown(trades, starting_equity),
    )


def group_by(
    trades: Sequence[TradeSnapshot],
    key: str,
    *,
    min_trades: int = 1,
) -> list[GroupStats]:
    """Срез статистики по атрибуту сделки.

    Сортировка по PnL, а не по количеству: вопрос «что приносит деньги»
    важнее, чем «чем я торгую чаще».
    """
    buckets: dict[str, list[TradeSnapshot]] = {}
    for trade in trades:
        value = getattr(trade, key, None)
        if value is None or value == "":
            value = "не указано"
        buckets.setdefault(str(value), []).append(trade)

    result = []
    for bucket_key, items in buckets.items():
        if len(items) < min_trades:
            continue
        wins = sum(1 for t in items if t.is_win)
        total_pnl = sum((t.pnl for t in items), ZERO)
        result.append(
            GroupStats(
                key=bucket_key,
                trades=len(items),
                wins=wins,
                win_rate=(
                    _safe_divide(Decimal(wins), Decimal(len(items))) * Decimal(100)
                ).quantize(PERCENT),
                total_pnl=total_pnl.quantize(MONEY),
                average_pnl=_safe_divide(
                    total_pnl, Decimal(len(items))
                ).quantize(MONEY),
            )
        )

    return sorted(result, key=lambda g: g.total_pnl, reverse=True)


def group_by_hour(
    trades: Sequence[TradeSnapshot], tz_offset_hours: int = 5
) -> list[GroupStats]:
    """Результат по часам в местном времени пользователя.

    Без сдвига таймзоны срез бесполезен: он показывал бы часы UTC, а
    решения принимаются по локальным.
    """
    from datetime import timedelta

    buckets: dict[str, list[TradeSnapshot]] = {}
    for trade in trades:
        local = trade.closed_at + timedelta(hours=tz_offset_hours)
        buckets.setdefault(f"{local.hour:02d}:00", []).append(trade)

    return _summarize_buckets(buckets)


def group_by_weekday(
    trades: Sequence[TradeSnapshot], tz_offset_hours: int = 5
) -> list[GroupStats]:
    from datetime import timedelta

    names = ["Понедельник", "Вторник", "Среда", "Четверг",
             "Пятница", "Суббота", "Воскресенье"]

    buckets: dict[str, list[TradeSnapshot]] = {}
    for trade in trades:
        local = trade.closed_at + timedelta(hours=tz_offset_hours)
        buckets.setdefault(names[local.weekday()], []).append(trade)

    return _summarize_buckets(buckets)


def _summarize_buckets(
    buckets: dict[str, list[TradeSnapshot]]
) -> list[GroupStats]:
    result = []
    for key, items in buckets.items():
        wins = sum(1 for t in items if t.is_win)
        total_pnl = sum((t.pnl for t in items), ZERO)
        result.append(
            GroupStats(
                key=key,
                trades=len(items),
                wins=wins,
                win_rate=(
                    _safe_divide(Decimal(wins), Decimal(len(items))) * Decimal(100)
                ).quantize(PERCENT),
                total_pnl=total_pnl.quantize(MONEY),
                average_pnl=_safe_divide(
                    total_pnl, Decimal(len(items))
                ).quantize(MONEY),
            )
        )
    return sorted(result, key=lambda g: g.total_pnl, reverse=True)
