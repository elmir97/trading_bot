"""Перечисления доменной модели.

Живут в domain-слое, а не рядом с моделями БД, потому что ими пользуются
и расчёты, и Telegram-клавиатуры, и биржевой адаптер. Модели БД их
импортируют, а не наоборот.
"""

from __future__ import annotations

from enum import StrEnum


class TradeSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def direction(self) -> int:
        """Множитель направления для формулы PnL: +1 для лонга, -1 для шорта."""
        return 1 if self is TradeSide.LONG else -1

    @property
    def label(self) -> str:
        return "🟢 LONG" if self is TradeSide.LONG else "🔴 SHORT"

    @property
    def opposite(self) -> TradeSide:
        return TradeSide.SHORT if self is TradeSide.LONG else TradeSide.LONG


class TradeStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class TradeSource(StrEnum):
    """Откуда пришла сделка.

    Различать важно: импортированные сделки содержат точные цифры биржи,
    но не содержат стратегии и причины входа, пока пользователь их не
    разметит. Статистика по стратегиям считается только по размеченным.
    """

    MANUAL = "MANUAL"
    IMPORTED = "IMPORTED"
    # Этап 15: сделка открыта ботом по подтверждённому пользователем сигналу.
    # Хранится как VARCHAR(16) без native enum — "SIGNAL_EXECUTION" (16
    # символов) укладывается в текущую длину колонки, миграция не нужна.
    SIGNAL_EXECUTION = "SIGNAL_EXECUTION"


class FillSide(StrEnum):
    """Направление исполнения: увеличивает позицию или уменьшает."""

    ENTRY = "ENTRY"
    EXIT = "EXIT"


class Timeframe(StrEnum):
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def minutes(self) -> int:
        return {
            Timeframe.M5: 5,
            Timeframe.M15: 15,
            Timeframe.M30: 30,
            Timeframe.H1: 60,
            Timeframe.H4: 240,
            Timeframe.D1: 1440,
        }[self]

    @property
    def higher(self) -> Timeframe | None:
        """Старший таймфрейм для проверки тренда.

        Соотношение примерно 1:4 — общепринятый шаг, при котором на старшем
        ТФ виден контекст, но он ещё не оторван от рабочего графика.
        """
        return {
            Timeframe.M5: Timeframe.M30,
            Timeframe.M15: Timeframe.H1,
            Timeframe.M30: Timeframe.H4,
            Timeframe.H1: Timeframe.H4,
            Timeframe.H4: Timeframe.D1,
            Timeframe.D1: None,
        }[self]


class SignalDirection(StrEnum):
    """WAIT — полноценный результат анализа, а не отсутствие ответа."""

    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


class MarketStructure(StrEnum):
    UPTREND = "UPTREND"          # HH + HL
    DOWNTREND = "DOWNTREND"      # LH + LL
    RANGE = "RANGE"              # без выраженной последовательности
    UNDEFINED = "UNDEFINED"      # данных недостаточно


class SignalLevel(StrEnum):
    """Готовность сетапа, обнаруженного фоновым сканером (этап 12)."""

    READY = "READY"      # все условия выполнены — можно входить
    FORMING = "FORMING"  # не хватает только подтверждающего паттерна


class SignalRecordStatus(StrEnum):
    """Жизненный цикл строки в таблице signals."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"


# --- Исполнение сделок по подтверждению (этап 15) --------------------------


class OrderSide(StrEnum):
    """Сторона биржевого ордера — не путать с TradeSide (стороной позиции)."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    STOP_MARKET = "STOP_MARKET"


class OrderRole(StrEnum):
    """Зачем этот ордер отправлен — один вход рождает до трёх строк."""

    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


class OrderStatus(StrEnum):
    """Статус исполнения ордера на бирже.

    UNKNOWN — отдельный терминальный статус для обрывов связи: сетевая
    ошибка или таймаут после отправки не позволяют считать ордер ни
    подтверждённым, ни отклонённым. См. app/execution/service.py (этап
    15.5) — повторная отправка при UNKNOWN запрещена, пока сверка по
    client_order_id не подтвердит, что ордера на бирже нет.

    DRY_RUN — этап 15.4: подтверждение получено, ордер прошёл все guard-ы,
    но на биржу ничего не уходит (отправка появится в 15.5) — строка лишь
    фиксирует, что именно ушло бы, для наблюдения по сводкам раздела 12а.

    REFUSED/DECLINED/EXPIRED — тоже этап 15.4, раздел 12а: три исхода
    попытки входа, ни один из которых не доходит до отправки на биржу.
    REFUSED — отказал гвард ещё до показа карточки подтверждения (какой
    именно — в error_code/error_message, туда же, что и будущий отказ
    биржи). DECLINED — пользователь нажал «Нет». EXPIRED — карточка
    прожила отведённые 60 секунд без ответа. Все три пишутся, чтобы
    ежедневная сводка исполнения считала решения пользователя и отказы
    кода из одной таблицы, не заводя под это отдельную.
    """

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"
    DRY_RUN = "DRY_RUN"
    REFUSED = "REFUSED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
