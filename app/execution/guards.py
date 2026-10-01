"""Общие предторговые проверки исполнения (этап 15.3, раздел 7 ТЗ).

Каждая check_* — чистая функция: получает уже готовые значения (кто их
раздобыл из БД/биржи — забота вызывающего кода) и возвращает None (проверка
пройдена) или ExecutionRefusal. Ничего не запрашивает и никуда не ходит —
поэтому каждую можно проверить тестом в одну строку, без моков.

02.10.2026: вход по сигналу удалён вместе с его проверками (сигнал, дрейф,
размер, RR, лимиты позиций и риска, маржа, белый список). Здесь остались
проверки, общие для любой отправки на биржу, — их переиспользует управление
позициями.
"""

from __future__ import annotations

from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.trading.enums import ExchangeKeyMode

# --- 1. EXECUTION_DISABLED --------------------------------------------------


def check_execution_enabled(*, execution_enabled: bool) -> ExecutionRefusal | None:
    if not execution_enabled:
        return ExecutionRefusal(Code.EXECUTION_DISABLED, "Исполнение сделок выключено.")
    return None


# --- 1а. LIVE_ORDERS_NOT_ALLOWED (раздел 16 ТЗ, шаг 15.5.1) -----------------
# Сразу после EXECUTION_DISABLED, до похода за правами ключа — реальные
# деньги на LIVE обязаны быть отдельным, явным включением
# (EXEC_ALLOW_LIVE_MODE_ORDERS), а не побочным следствием того, что
# EXECUTION_DISABLED уже False.


def check_live_orders_allowed(
    *, trading_mode: str, allow_live_mode_orders: bool
) -> ExecutionRefusal | None:
    if trading_mode == "live" and not allow_live_mode_orders:
        return ExecutionRefusal(
            Code.LIVE_ORDERS_NOT_ALLOWED,
            "Отправка реальных ордеров на LIVE выключена конфигом "
            "(EXEC_ALLOW_LIVE_MODE_ORDERS).",
        )
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
# Права проверяются вызывающим кодом один раз, результат приходит уже
# готовым булем (см. app/services/permissions.py) — здесь только решение,
# отказывать или нет.


def check_permissions_trustworthy(*, trustworthy: bool) -> ExecutionRefusal | None:
    if not trustworthy:
        return ExecutionRefusal(Code.PERMISSIONS_UNKNOWN, "Не удалось проверить права ключа.")
    return None


# --- POSITION_MODE_UNKNOWN (раздел 16 ТЗ, шаг 15.5.1) -----------------------
# По тому же образцу, что PERMISSIONS_UNKNOWN выше: режим позиций читается
# вызывающим кодом, результат приходит уже готовым булем-или-None (см.
# app/services/position_mode.py) — здесь только решение, отказывать или нет.


def check_position_mode_known(*, known: bool) -> ExecutionRefusal | None:
    if not known:
        return ExecutionRefusal(
            Code.POSITION_MODE_UNKNOWN, "Не удалось проверить режим позиций аккаунта."
        )
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
