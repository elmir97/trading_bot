"""Проверка сделки на соответствие торговому плану.

Принцип: предупреждать, но не запрещать. Жёсткий запрет привёл бы к тому,
что нарушающие план сделки просто перестали бы попадать в журнал, и данные
перестали бы отражать реальную торговлю — то есть инструмент потерял бы
смысл именно в тех случаях, ради которых он нужен.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.database.models.trading_plan import TradingPlan
from app.database.repositories.trade import TradeRepository
from app.trading.calculations import (
    CalculationError,
    calculate_risk_reward,
    stop_distance,
)
from app.trading.enums import TradeSide

ZERO = Decimal(0)


class ViolationCode(StrEnum):
    NO_STOP_LOSS = "NO_STOP_LOSS"
    RISK_TOO_HIGH = "RISK_TOO_HIGH"
    LOW_RISK_REWARD = "LOW_RISK_REWARD"
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
    TIMEFRAME_NOT_ALLOWED = "TIMEFRAME_NOT_ALLOWED"
    LEVERAGE_TOO_HIGH = "LEVERAGE_TOO_HIGH"
    DAILY_TRADE_LIMIT = "DAILY_TRADE_LIMIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"


@dataclass(frozen=True, slots=True)
class Violation:
    code: ViolationCode
    message: str
    is_blocking: bool = False


@dataclass(slots=True)
class PlanCheck:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def has_blocking(self) -> bool:
        """Блокирующими считаются только достигнутые лимиты убытка.

        Это не запрет на сделку, а требование явного подтверждения:
        торговля после исчерпания дневного лимита — самый дорогой из
        типовых сценариев потери депозита.
        """
        return any(v.is_blocking for v in self.violations)

    def render(self) -> str:
        if self.ok:
            return "✅ Сделка соответствует торговому плану."
        lines = ["⚠️ <b>Сделка нарушает торговый план</b>", ""]
        lines.extend(f"• {v.message}" for v in self.violations)
        return "\n".join(lines)


# Пока поддерживаются фиксированные смещения — для этих зон перехода на
# летнее время нет, так что это корректно. При добавлении пользователей из
# зон с DST здесь понадобится zoneinfo (см. tz_offset_for).
KNOWN_TZ_OFFSETS: dict[str, int] = {
    "Asia/Yekaterinburg": 5,
    "Europe/Moscow": 3,
    "UTC": 0,
    "Europe/Kaliningrad": 2,
    "Asia/Novosibirsk": 7,
    "Asia/Vladivostok": 10,
}


def tz_offset_for(timezone: str | None, default: int = 5) -> int:
    """Сдвиг часового пояса в часах по IANA-имени из user_settings.timezone.

    Общая точка для интерактивных хендлеров (статистика, отчёты) и фоновых
    задач (этап 12) — раньше эта таблица была продублирована в
    app/bot/handlers/statistics.py.
    """
    if timezone is None:
        return default
    return KNOWN_TZ_OFFSETS.get(timezone, default)


def day_bounds(now: datetime, tz_offset_hours: int = 5) -> tuple[datetime, datetime]:
    """Границы торгового дня в часовом поясе пользователя, в UTC.

    Без учёта таймзоны «сегодня» на UTC+5 начиналось бы в 5 утра по
    местному времени, и вечерние сделки попадали бы в следующий день.
    """
    local = now + timedelta(hours=tz_offset_hours)
    local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_start - timedelta(hours=tz_offset_hours)
    return start_utc, start_utc + timedelta(days=1)


def week_bounds(now: datetime, tz_offset_hours: int = 5) -> tuple[datetime, datetime]:
    day_start, _ = day_bounds(now, tz_offset_hours)
    local = now + timedelta(hours=tz_offset_hours)
    week_start = day_start - timedelta(days=local.weekday())
    return week_start, week_start + timedelta(days=7)


class PlanValidator:
    def __init__(self, trades: TradeRepository, tz_offset_hours: int = 5) -> None:
        self._trades = trades
        self._tz_offset = tz_offset_hours

    async def check(
        self,
        *,
        plan: TradingPlan,
        user_id: int,
        symbol: str,
        side: TradeSide,
        entry_price: Decimal,
        quantity: Decimal,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
        leverage: int,
        timeframe: str | None,
        account_balance: Decimal | None,
        now: datetime | None = None,
    ) -> PlanCheck:
        result = PlanCheck()
        moment = now or datetime.now(UTC)

        # --- Стоп-лосс -----------------------------------------------------
        if stop_loss is None:
            result.violations.append(
                Violation(
                    ViolationCode.NO_STOP_LOSS,
                    "Стоп-лосс не задан. Методология запрещает сделки без стопа.",
                )
            )

        # --- Риск ----------------------------------------------------------
        if stop_loss is not None and account_balance and account_balance > ZERO:
            try:
                distance = stop_distance(
                    entry_price=entry_price, stop_loss=stop_loss, side=side
                )
                risk_percent = distance * quantity / account_balance * Decimal(100)
                if risk_percent > plan.risk_per_trade_percent:
                    result.violations.append(
                        Violation(
                            ViolationCode.RISK_TOO_HIGH,
                            f"Риск {risk_percent:.2f}%, допустимый — "
                            f"{plan.risk_per_trade_percent:g}%.",
                        )
                    )
            except CalculationError:
                pass  # некорректный стоп уже отловит журнал

        # --- Risk/Reward ---------------------------------------------------
        if stop_loss is not None and take_profit is not None:
            try:
                rr = calculate_risk_reward(
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    side=side,
                )
                if rr < plan.min_risk_reward:
                    result.violations.append(
                        Violation(
                            ViolationCode.LOW_RISK_REWARD,
                            f"RR 1:{rr:g}, минимальный по плану — "
                            f"1:{plan.min_risk_reward:g}.",
                        )
                    )
            except CalculationError:
                pass

        # --- Инструмент и таймфрейм ----------------------------------------
        if plan.allowed_symbols and symbol.upper() not in plan.allowed_symbols:
            result.violations.append(
                Violation(
                    ViolationCode.SYMBOL_NOT_ALLOWED,
                    f"{symbol} вне списка разрешённых инструментов "
                    f"({', '.join(plan.allowed_symbols)}).",
                )
            )

        if (
            timeframe
            and plan.allowed_timeframes
            and timeframe not in plan.allowed_timeframes
        ):
            result.violations.append(
                Violation(
                    ViolationCode.TIMEFRAME_NOT_ALLOWED,
                    f"Таймфрейм {timeframe} вне торгового плана "
                    f"({', '.join(plan.allowed_timeframes)}).",
                )
            )

        if leverage > plan.max_leverage:
            result.violations.append(
                Violation(
                    ViolationCode.LEVERAGE_TOO_HIGH,
                    f"Плечо {leverage}x выше допустимого {plan.max_leverage}x.",
                )
            )

        # --- Лимиты дня и недели -------------------------------------------
        day_start, day_end = day_bounds(moment, self._tz_offset)
        trades_today = await self._trades.count_opened_between(
            user_id, day_start, day_end
        )
        if trades_today >= plan.max_trades_per_day:
            result.violations.append(
                Violation(
                    ViolationCode.DAILY_TRADE_LIMIT,
                    f"Сегодня уже {trades_today} сделок при лимите "
                    f"{plan.max_trades_per_day}.",
                )
            )

        if account_balance and account_balance > ZERO:
            day_pnl = await self._trades.sum_pnl_between(user_id, day_start, day_end)
            if day_pnl is not None:
                day_loss_pct = -day_pnl / account_balance * Decimal(100)
                if day_loss_pct >= plan.max_daily_loss_percent:
                    result.violations.append(
                        Violation(
                            ViolationCode.DAILY_LOSS_LIMIT,
                            f"Дневной лимит убытка достигнут: "
                            f"−{day_loss_pct:.2f}% при лимите "
                            f"{plan.max_daily_loss_percent:g}%. "
                            f"Торговый день стоит закрыть.",
                            is_blocking=True,
                        )
                    )

            week_start, week_end = week_bounds(moment, self._tz_offset)
            week_pnl = await self._trades.sum_pnl_between(
                user_id, week_start, week_end
            )
            if week_pnl is not None:
                week_loss_pct = -week_pnl / account_balance * Decimal(100)
                if week_loss_pct >= plan.max_weekly_loss_percent:
                    result.violations.append(
                        Violation(
                            ViolationCode.WEEKLY_LOSS_LIMIT,
                            f"Недельный лимит убытка достигнут: "
                            f"−{week_loss_pct:.2f}% при лимите "
                            f"{plan.max_weekly_loss_percent:g}%.",
                            is_blocking=True,
                        )
                    )

        return result
