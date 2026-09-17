"""Предторговые проверки перед отправкой ордера (этап 15.3, раздел 7 ТЗ).

Каждая check_* — чистая функция: получает уже готовые значения (кто их
раздобыл из БД/биржи — забота будущего service.py, раздел 1 ТЗ) и
возвращает None (проверка пройдена) или ExecutionRefusal. Ничего не
запрашивает и никуда не ходит — поэтому каждую можно проверить тестом
в одну строку, без моков.

run_guards() прогоняет их в порядке раздела 7 и останавливается на первом
отказе: "первая сработавшая останавливает вход". Именно этот порядок
проверяет test_guards.py::TestGuardOrder.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.exchanges.base import SymbolInfo
from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.sizing import SizingResult, calculate_size
from app.trading.calculations import CalculationError, calculate_risk_reward, stop_distance
from app.trading.enums import ExchangeKeyMode, TradeSide

ZERO = Decimal(0)


# --- 1. EXECUTION_DISABLED --------------------------------------------------


def check_execution_enabled(*, execution_enabled: bool) -> ExecutionRefusal | None:
    if not execution_enabled:
        return ExecutionRefusal(Code.EXECUTION_DISABLED, "Исполнение сделок выключено.")
    return None


# --- 2. NO_TRADING_KEY -------------------------------------------------------


def check_trading_key(
    *, has_key: bool, key_can_trade_futures: bool
) -> ExecutionRefusal | None:
    if not has_key:
        return ExecutionRefusal(Code.NO_TRADING_KEY, "Нет ключа BingX для этого пользователя.")
    if not key_can_trade_futures:
        return ExecutionRefusal(
            Code.NO_TRADING_KEY, "Ключ BingX без права на фьючерсную торговлю."
        )
    return None


# --- PERMISSIONS_UNKNOWN (раздел 8 ТЗ) --------------------------------------
# Не входит в нумерованный раздел 7 и run_guards(): права проверяются один
# раз в ExecutionService.evaluate(), до похода за тикером, и результат
# приходит уже готовым булем (см. app/services/permissions.py) — здесь
# только решение, отказывать или нет.


def check_permissions_trustworthy(*, trustworthy: bool) -> ExecutionRefusal | None:
    if not trustworthy:
        return ExecutionRefusal(Code.PERMISSIONS_UNKNOWN, "Не удалось проверить права ключа.")
    return None


# --- 2а. MODE_NOT_ALLOWED (этап 15.4в) ---------------------------------------

_ACCOUNT_LABEL = {
    ExchangeKeyMode.LIVE: "реальном счёте",
    ExchangeKeyMode.DEMO: "демо-счёте",
}


def check_mode_allowed(
    *, selected_mode: ExchangeKeyMode, allowed_mode: ExchangeKeyMode
) -> ExecutionRefusal | None:
    """Счёт, показанный в настройках (UserSettings.active_exchange_mode), и
    счёт, куда реально уходят ордера (Settings.bingx_trading_mode), обязаны
    совпадать — иначе пользователь подтверждает вход по цифрам одного
    счёта, а ордер ушёл бы на другой. Молчаливого исполнения на "не тот"
    счёт нет: рассинхрон блокирует вход целиком, симметрично в обе стороны.
    """
    if selected_mode is not allowed_mode:
        return ExecutionRefusal(
            Code.MODE_NOT_ALLOWED,
            f"Исполнение разрешено только на {_ACCOUNT_LABEL[allowed_mode]}. "
            "Переключи счёт в «Настройках».",
        )
    return None


# --- 3. SIGNAL_EXPIRED -------------------------------------------------------


def check_signal_not_expired(
    *, expires_at: datetime, now: datetime
) -> ExecutionRefusal | None:
    if now >= expires_at:
        return ExecutionRefusal(Code.SIGNAL_EXPIRED, "Сигнал уже истёк.")
    return None


# --- 4. SIGNAL_ALREADY_USED --------------------------------------------------


def check_signal_not_used(
    *, trade_opened_at: datetime | None
) -> ExecutionRefusal | None:
    if trade_opened_at is not None:
        return ExecutionRefusal(
            Code.SIGNAL_ALREADY_USED, "По этому сигналу уже открывали сделку."
        )
    return None


# --- 5. POSITION_EXISTS -------------------------------------------------------


def check_no_existing_position(*, has_open_position: bool) -> ExecutionRefusal | None:
    if has_open_position:
        return ExecutionRefusal(
            Code.POSITION_EXISTS, "По этому символу уже есть открытая позиция."
        )
    return None


# --- 6. MAX_POSITIONS ---------------------------------------------------------


def check_max_positions(
    *, open_positions_count: int, max_positions: int
) -> ExecutionRefusal | None:
    if open_positions_count >= max_positions:
        return ExecutionRefusal(
            Code.MAX_POSITIONS,
            f"Уже {open_positions_count} открытых позиций при лимите {max_positions}.",
        )
    return None


# --- 7. MAX_TOTAL_RISK --------------------------------------------------------


def check_max_total_risk(
    *,
    current_total_risk_percent: Decimal,
    new_risk_percent: Decimal,
    max_total_risk_percent: Decimal,
) -> ExecutionRefusal | None:
    total = current_total_risk_percent + new_risk_percent
    if total > max_total_risk_percent:
        return ExecutionRefusal(
            Code.MAX_TOTAL_RISK,
            f"Суммарный риск {total:g}% превысит лимит {max_total_risk_percent:g}%.",
        )
    return None


# --- 8. DAILY_LOSS_LIMIT ------------------------------------------------------


def check_daily_loss_limit(
    *, day_loss_percent: Decimal | None, max_daily_loss_percent: Decimal
) -> ExecutionRefusal | None:
    if day_loss_percent is not None and day_loss_percent >= max_daily_loss_percent:
        return ExecutionRefusal(
            Code.DAILY_LOSS_LIMIT,
            f"Дневной лимит убытка достигнут: -{day_loss_percent:g}%.",
        )
    return None


# --- 9. PRICE_DRIFT ------------------------------------------------------------


def check_price_drift(
    *,
    planned_price: Decimal,
    current_price: Decimal,
    stop_loss: Decimal,
    max_drift_ratio: Decimal,
) -> ExecutionRefusal | None:
    drift = abs(current_price - planned_price)
    allowed = abs(planned_price - stop_loss) * max_drift_ratio
    if drift > allowed:
        return ExecutionRefusal(
            Code.PRICE_DRIFT,
            f"Цена ушла на {drift:g} от расчётной {planned_price:g} "
            f"(допустимо {allowed:g}).",
        )
    return None


# --- 10. INVALID_LEVELS ---------------------------------------------------------


def check_valid_levels(
    *,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    side: TradeSide,
    min_risk_reward: Decimal,
) -> ExecutionRefusal | None:
    """entry_price здесь — живая цена биржи на момент проверки, а не цена
    входа из сигнала (та зафиксирована в signal.entry_low/entry_high и сюда
    не попадает). calculate_risk_reward() сама формулирует ошибки в терминах
    "цены входа" — для сигнала это верно, для гварда нет, поэтому её
    CalculationError не пробрасывается как есть: stop_distance() и сам
    calculate_risk_reward() зовутся раздельно, каждая ветка отказа получает
    свой текст с живой ценой и уровнем явными числами."""
    try:
        stop_distance(entry_price=entry_price, stop_loss=stop_loss, side=side)
    except CalculationError:
        return ExecutionRefusal(
            Code.INVALID_LEVELS,
            f"Цена ушла за стоп: сейчас {entry_price:g}, стоп {stop_loss:g}.",
        )
    try:
        rr = calculate_risk_reward(
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            side=side,
        )
    except CalculationError:
        return ExecutionRefusal(
            Code.INVALID_LEVELS,
            f"Цена уже прошла тейк: сейчас {entry_price:g}, тейк {take_profit:g}.",
        )
    if rr < min_risk_reward:
        return ExecutionRefusal(
            Code.INVALID_LEVELS,
            f"RR 1:{rr:g} ниже минимального 1:{min_risk_reward:g} "
            f"(пересчитан по текущей цене {entry_price:g}, не по цене сигнала на карточке).",
        )
    return None


# --- 11. SIZE_TOO_SMALL / INSUFFICIENT_MARGIN ------------------------------


def check_size(*, sizing: SizingResult | ExecutionRefusal) -> ExecutionRefusal | None:
    return sizing if isinstance(sizing, ExecutionRefusal) else None


# --- 12. SYMBOL_NOT_ALLOWED ---------------------------------------------------


def check_symbol_allowed(
    *, symbol: str, whitelist: Sequence[str]
) -> ExecutionRefusal | None:
    if whitelist and symbol.upper() not in {s.upper() for s in whitelist}:
        return ExecutionRefusal(
            Code.SYMBOL_NOT_ALLOWED, f"{symbol} вне списка разрешённых инструментов."
        )
    return None


# --- Оркестрация порядка (без I/O — все данные уже собраны вызывающим) -----


@dataclass(frozen=True, slots=True, kw_only=True)
class GuardInputs:
    """Один снимок данных для всех 12 проверок раздела 7, в порядке ТЗ."""

    # 1
    execution_enabled: bool
    # 2
    has_trading_key: bool
    key_can_trade_futures: bool
    # 2а (этап 15.4в)
    selected_exchange_mode: ExchangeKeyMode
    allowed_exchange_mode: ExchangeKeyMode
    # 3
    signal_expires_at: datetime
    now: datetime
    # 4
    signal_trade_opened_at: datetime | None
    # 5
    has_open_position: bool
    # 6
    open_positions_count: int
    max_positions: int
    # 7
    current_total_risk_percent: Decimal
    new_risk_percent: Decimal
    max_total_risk_percent: Decimal
    # 8
    day_loss_percent: Decimal | None
    max_daily_loss_percent: Decimal
    # 9
    planned_price: Decimal
    current_price: Decimal
    max_price_drift_ratio: Decimal
    # 10 и 11 (стоп/тейк/объём считаются от entry_price — фактической цены
    # входа на момент подтверждения, не от planned_price выше)
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    side: TradeSide
    min_risk_reward: Decimal
    account_balance: Decimal
    leverage: int
    symbol_info: SymbolInfo
    # 12
    symbol: str
    symbol_whitelist: Sequence[str]


def run_guards(inputs: GuardInputs) -> ExecutionRefusal | None:
    if refusal := check_execution_enabled(execution_enabled=inputs.execution_enabled):
        return refusal
    if refusal := check_trading_key(
        has_key=inputs.has_trading_key, key_can_trade_futures=inputs.key_can_trade_futures
    ):
        return refusal
    if refusal := check_mode_allowed(
        selected_mode=inputs.selected_exchange_mode,
        allowed_mode=inputs.allowed_exchange_mode,
    ):
        return refusal
    if refusal := check_signal_not_expired(expires_at=inputs.signal_expires_at, now=inputs.now):
        return refusal
    if refusal := check_signal_not_used(trade_opened_at=inputs.signal_trade_opened_at):
        return refusal
    if refusal := check_no_existing_position(has_open_position=inputs.has_open_position):
        return refusal
    if refusal := check_max_positions(
        open_positions_count=inputs.open_positions_count, max_positions=inputs.max_positions
    ):
        return refusal
    if refusal := check_max_total_risk(
        current_total_risk_percent=inputs.current_total_risk_percent,
        new_risk_percent=inputs.new_risk_percent,
        max_total_risk_percent=inputs.max_total_risk_percent,
    ):
        return refusal
    if refusal := check_daily_loss_limit(
        day_loss_percent=inputs.day_loss_percent,
        max_daily_loss_percent=inputs.max_daily_loss_percent,
    ):
        return refusal
    if refusal := check_price_drift(
        planned_price=inputs.planned_price,
        current_price=inputs.current_price,
        stop_loss=inputs.stop_loss,
        max_drift_ratio=inputs.max_price_drift_ratio,
    ):
        return refusal
    if refusal := check_valid_levels(
        entry_price=inputs.entry_price,
        stop_loss=inputs.stop_loss,
        take_profit=inputs.take_profit,
        side=inputs.side,
        min_risk_reward=inputs.min_risk_reward,
    ):
        return refusal
    if refusal := check_size(
        sizing=calculate_size(
            account_balance=inputs.account_balance,
            risk_percent=inputs.new_risk_percent,
            entry_price=inputs.entry_price,
            stop_loss=inputs.stop_loss,
            side=inputs.side,
            leverage=inputs.leverage,
            symbol_info=inputs.symbol_info,
        )
    ):
        return refusal
    if refusal := check_symbol_allowed(symbol=inputs.symbol, whitelist=inputs.symbol_whitelist):
        return refusal
    return None
