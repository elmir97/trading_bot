"""Отказ во входе (этап 15.3) и OrderRequest (этап 15.4, раздел 1 ТЗ).

OrderResult из раздела 1 ТЗ сюда не заводим: у него нет потребителя, пока
service.py не начнёт реально отправлять ордера (этап 15.5) — до тех пор
это был бы датакласс без единой точки использования.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.trading.enums import OrderRole, OrderSide, TradeSide


class ExecutionRefusalCode(StrEnum):
    """Раздел 7 ТЗ. Большинство значений — порядок проверки в run_guards();
    PERMISSIONS_UNKNOWN, POSITION_MODE_UNKNOWN и SYMBOL_DATA_UNAVAILABLE
    в него не входят — это отказы, которые ExecutionService.evaluate()
    строит сам, до/помимо run_guards()."""

    EXECUTION_DISABLED = "EXECUTION_DISABLED"
    # Раздел 16 ТЗ, шаг 15.5.1: LIVE запрещён конфигом (EXEC_ALLOW_LIVE_
    # MODE_ORDERS=false, дефолт) — сразу после EXECUTION_DISABLED, до
    # похода за правами ключа. В run_guards() входит (см. "1а" в guards.py),
    # в отличие от PERMISSIONS_UNKNOWN/POSITION_MODE_UNKNOWN ниже.
    LIVE_ORDERS_NOT_ALLOWED = "LIVE_ORDERS_NOT_ALLOWED"
    # Раздел 8 ТЗ: права ключа не удалось проверить (сбой запроса к бирже,
    # отметка отсутствует или протухла) — отдельно от NO_TRADING_KEY, у
    # которого права ИЗВЕСТНЫ и они недостаточны. См. app/services/permissions.py.
    PERMISSIONS_UNKNOWN = "PERMISSIONS_UNKNOWN"
    # Раздел 16 ТЗ, шаг 15.5.1: режим позиций (хедж/односторонний) не
    # удалось прочитать (сбой запроса к бирже) — по тому же принципу, что
    # и PERMISSIONS_UNKNOWN: сбой не означает "можно", positionSide без
    # известного режима невозможно собрать правильно. См.
    # app/services/position_mode.py.
    POSITION_MODE_UNKNOWN = "POSITION_MODE_UNKNOWN"
    NO_TRADING_KEY = "NO_TRADING_KEY"
    # Этап 15.4в: показанный в настройках счёт (LIVE/DEMO) не совпадает с
    # тем, что разрешён конфигом (Settings.bingx_trading_mode) — см.
    # guards.check_mode_allowed.
    MODE_NOT_ALLOWED = "MODE_NOT_ALLOWED"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    SIGNAL_ALREADY_USED = "SIGNAL_ALREADY_USED"
    POSITION_EXISTS = "POSITION_EXISTS"
    MAX_POSITIONS = "MAX_POSITIONS"
    MAX_TOTAL_RISK = "MAX_TOTAL_RISK"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    PRICE_DRIFT = "PRICE_DRIFT"
    # Пакет B: сигнал устарел ценой — рынок ушёл от опорной цены сигнала в
    # сторону тейка дальше EXEC_MAX_SIGNAL_STALENESS_RATIO. Не то же самое,
    # что PRICE_DRIFT (тот сравнивает с ценой на карточке, а не с сигналом) —
    # см. guards.check_signal_not_stale.
    SIGNAL_STALE = "SIGNAL_STALE"
    INVALID_LEVELS = "INVALID_LEVELS"
    SIZE_TOO_SMALL = "SIZE_TOO_SMALL"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
    # Инструмент в вайтлисте, но биржа не отдала по нему SymbolInfo (список
    # инструментов пуст/не содержит символ) — не то же самое, что осознанный
    # отказ вне вайтлиста (SYMBOL_NOT_ALLOWED). См. ExecutionService.evaluate().
    SYMBOL_DATA_UNAVAILABLE = "SYMBOL_DATA_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ExecutionRefusal:
    code: ExecutionRefusalCode
    message: str


def client_order_id(*, signal_id: int, user_id: int, role: OrderRole) -> str:
    """Детерминированный ключ идемпотентности (раздел 8 ТЗ):
    f"tj{signal_id}u{user_id}{role.letter}". Один и тот же вызов с теми же
    аргументами всегда даёт одну и ту же строку — этим и обеспечивается
    "повторной отправки нет, пока сверка по client_order_id не подтвердит
    обратное" (раздел 8, шаг 5), а не полагается на память процесса.

    Раздел 16 ТЗ, шаг 15.5.2: разделитель "u" перед user_id обязателен —
    без него f"tj{signal_id}{user_id}..." неоднозначен: (signal_id=12,
    user_id=3) и (signal_id=1, user_id=23) склеивались бы в одну и ту же
    строку "tj123...", а UNIQUE на client_order_id — глобальный по всей
    таблице, не по паре (signal_id, user_id). role.letter — не role.value:
    тот содержит "_" ("STOP_LOSS"), это не только ASCII буквы и цифры.
    Длина на максимумах Integer-колонок id (2**31-1 = 10 цифр у обоих):
    2 ("tj") + 10 + 1 ("u") + 10 + 1 (буква роли) = 24 символа — с запасом
    от кода в BingXClient.place_market_order (1-40, раздел 16 ТЗ) и от
    более консервативных лимитов, встречающихся у бирж этого семейства
    API; точный лимит BingX живьём не снят (раздел 16, открыто до 15.5.5).
    """
    return f"tj{signal_id}u{user_id}{role.letter}"


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderRequest:
    """Всё, что ушло бы на биржу при входе по сигналу (раздел 1 и 9 ТЗ).

    Один вход — маркет на вход со вложенными TP/SL (раздел 9), но на
    уровне БД (execution_orders) это три отдельные строки с разными
    role/client_order_id (раздел 8) — см. service.build_execution_orders().
    Ничего из этого не относится к реальной отправке: на этапе 15.4
    OrderRequest только описывает намерение для сухого прогона и лога.
    """

    user_id: int
    signal_id: int
    symbol: str
    side: OrderSide             # BUY/SELL — сторона ВХОДНОГО ордера
    position_side: TradeSide    # LONG/SHORT — сторона позиции
    quantity: Decimal
    entry_price: Decimal        # цена на момент подтверждения (справочно — вход маркетом)
    leverage: int
    stop_loss: Decimal
    take_profit: Decimal
    notional: Decimal
    margin: Decimal
    risk_amount: Decimal
    risk_percent: Decimal
    risk_reward: Decimal

    @property
    def entry_client_order_id(self) -> str:
        return client_order_id(signal_id=self.signal_id, user_id=self.user_id, role=OrderRole.ENTRY)

    @property
    def stop_loss_client_order_id(self) -> str:
        return client_order_id(
            signal_id=self.signal_id, user_id=self.user_id, role=OrderRole.STOP_LOSS
        )

    @property
    def take_profit_client_order_id(self) -> str:
        return client_order_id(
            signal_id=self.signal_id, user_id=self.user_id, role=OrderRole.TAKE_PROFIT
        )

    def render(self) -> str:
        """Полное содержимое для сообщения «сухой прогон» и для лога
        (раздел 12а ТЗ: «готовый OrderRequest в лог»)."""
        return (
            f"symbol={self.symbol} side={self.side.value} "
            f"position_side={self.position_side.value} quantity={self.quantity:g} "
            f"entry≈{self.entry_price:g} leverage={self.leverage}x "
            f"stop_loss={self.stop_loss:g} take_profit={self.take_profit:g} "
            f"notional={self.notional:g} margin={self.margin:g} "
            f"risk_amount={self.risk_amount:g} risk_percent={self.risk_percent:g}% "
            f"rr=1:{self.risk_reward:g} "
            f"client_order_id[entry]={self.entry_client_order_id} "
            f"client_order_id[sl]={self.stop_loss_client_order_id} "
            f"client_order_id[tp]={self.take_profit_client_order_id}"
        )
