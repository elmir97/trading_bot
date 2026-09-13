"""Ежедневная сводка исполнения (этап 15.4, раздел 12а ТЗ).

Наблюдение за этапом ведёт код, а не человек глазами: карточка
подтверждения (app/bot/handlers/execution.py) и ExecutionService.evaluate()
(app/execution/service.py) уже пишут по одной строке в execution_orders
на каждый исход попытки входа по READY-сигналу —

    DRY_RUN   — подтверждено ("Да", прошло все guard-ы)
    DECLINED  — пользователь нажал "Нет"
    EXPIRED   — карточка прожила 60 секунд без ответа
    REFUSED   — отказал guard ещё до показа карточки (error_code — какой)

Этот модуль только читает эти строки и считает: отдельной таблицы под
статистику нет и не заводится — дублировать то, что уже пишется в
execution_orders, значит рано или поздно разойтись с ним. build_stats() и
render_execution_digest() — чистые функции без I/O, поэтому проверяются
тестами без БД (tests/test_execution_digest.py); DailyJobs (app/workers/daily.py)
только достаёт строки за сутки и вызывает их.

"Сигналов READY" в шапке сводки — исключение: это count() из signals
(SignalRepository.count_ready_notified_between()), не из execution_orders,
потому что отказ гварда происходит до появления карточки, а карточка
(и, значит, execution_orders-строка) при этом ещё не существует —
без отдельного источника READY-сигналы, упёршиеся в гвард, были бы не
видны. build_stats() принимает готовое число ready_signals параметром,
I/O остаётся в DailyJobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.database.models.execution_order import ExecutionOrder
from app.trading.enums import OrderStatus
from app.workers.base import fmt_decimal

ZERO = Decimal(0)

# "расчётный риск отклонился от заданного больше чем на 10%" (раздел 12а).
RISK_DEVIATION_RATIO = Decimal("0.10")
# "объём после округления даёт риск меньше половины заданного" — отдельная,
# более серьёзная планка внутри той же деформации от округления лота вниз.
RISK_UNDERSIZED_RATIO = Decimal("0.50")
# "гвард срабатывает подозрительно часто (более половины сигналов)" — доля
# от ВСЕХ попыток за сутки (показанные карточки + отказы гвардов), не
# только от отказов.
GUARD_DOMINANCE_RATIO = Decimal("0.50")
# Ниже этого числа попыток доля гварда не считается — иначе "1 из 1"
# печатается как "подозрительно часто" на одном событии.
GUARD_DOMINANCE_MIN_ATTEMPTS = 5
# Ниже этого числа карточек с данными о дрейфе средний дрейф не считается —
# та же защита от вывода по одному-двум наблюдениям.
PRICE_DRIFT_MIN_CARDS = 3


@dataclass(slots=True)
class RiskDeviation:
    symbol: str
    actual_percent: Decimal
    target_percent: Decimal


@dataclass(slots=True)
class ExecutionDigestStats:
    """Итог за сутки — только числа, без форматирования (раздел 12а)."""

    # Из signals (SignalRepository.count_ready_notified_between), не из
    # execution_orders — сколько раз READY-сетап реально дошёл до
    # пользователя уведомлением, независимо от того, нажималась ли кнопка
    # и прошла ли попытка гвардов (раздел 12а).
    ready_signals: int = 0

    confirmed: int = 0
    declined: int = 0
    expired: int = 0
    refusals_by_code: dict[str, int] = field(default_factory=dict)

    # Только у подтверждённых (DRY_RUN) есть реальный объём/маржа/RR —
    # только по ним считаются "средние" в сводке.
    confirmed_risk_percents: list[Decimal] = field(default_factory=list)
    confirmed_risk_rewards: list[Decimal] = field(default_factory=list)
    confirmed_drift_percents: list[Decimal] = field(default_factory=list)

    risk_deviations: list[RiskDeviation] = field(default_factory=list)
    undersized: list[RiskDeviation] = field(default_factory=list)

    @property
    def total_cards(self) -> int:
        """Показанных карточек — сумма трёх исходов. Отказы гвардов сюда
        не входят: карточка при них ещё не появляется (раздел 7 ТЗ)."""
        return self.confirmed + self.declined + self.expired

    @property
    def total_refusals(self) -> int:
        return sum(self.refusals_by_code.values())

    @property
    def total_attempts(self) -> int:
        return self.total_cards + self.total_refusals


def build_stats(
    rows: list[ExecutionOrder],
    *,
    target_risk_percent: Decimal | None,
    ready_signals: int = 0,
) -> ExecutionDigestStats:
    """rows — строки execution_orders (role=ENTRY) за сутки одного
    пользователя, см. ExecutionOrderRepository.list_entries_between().
    target_risk_percent — текущий risk_per_trade_percent торгового плана,
    точка отсчёта для "риск отклонился от заданного" (раздел 12а).
    ready_signals — SignalRepository.count_ready_notified_between() за то
    же окно: считается отдельно от rows, источник другой (signals, не
    execution_orders), поэтому передаётся готовым числом, а не строками."""
    stats = ExecutionDigestStats(ready_signals=ready_signals)

    for row in rows:
        if row.status is OrderStatus.DRY_RUN:
            stats.confirmed += 1
            if row.risk_percent is not None:
                stats.confirmed_risk_percents.append(row.risk_percent)
                _check_risk_deviation(stats, row, target_risk_percent)
            if row.risk_reward is not None:
                stats.confirmed_risk_rewards.append(row.risk_reward)
            if row.price_drift_percent is not None:
                stats.confirmed_drift_percents.append(row.price_drift_percent)
        elif row.status is OrderStatus.DECLINED:
            stats.declined += 1
        elif row.status is OrderStatus.EXPIRED:
            stats.expired += 1
        elif row.status is OrderStatus.REFUSED:
            code = row.error_code or "?"
            stats.refusals_by_code[code] = stats.refusals_by_code.get(code, 0) + 1

    return stats


def _check_risk_deviation(
    stats: ExecutionDigestStats, row: ExecutionOrder, target_risk_percent: Decimal | None
) -> None:
    if target_risk_percent is None or target_risk_percent <= ZERO or row.risk_percent is None:
        return
    # Округление лота всегда вниз (раздел 6 ТЗ) — фактический риск не
    # может оказаться больше заданного, отклонение здесь почти всегда
    # "риск меньше цели", но считаем по модулю на случай смены плана
    # в течение дня (сравниваем с текущим значением, не тем, что было
    # на момент входа).
    deviation_ratio = abs(target_risk_percent - row.risk_percent) / target_risk_percent
    if deviation_ratio > RISK_DEVIATION_RATIO:
        deviation = RiskDeviation(row.symbol, row.risk_percent, target_risk_percent)
        stats.risk_deviations.append(deviation)
        if deviation_ratio > RISK_UNDERSIZED_RATIO:
            stats.undersized.append(deviation)


def detect_anomalies(stats: ExecutionDigestStats, *, max_price_drift_ratio: Decimal) -> list[str]:
    """Раздел 12а ТЗ — единственный раздел сводки, который человек глазами
    бы не поймал. Возвращает готовые строки-пункты, порядок — как в ТЗ."""
    anomalies: list[str] = []

    for d in stats.risk_deviations:
        anomalies.append(
            f"риск по {d.symbol} отклонился от заданных {fmt_decimal(d.target_percent)}%: "
            f"получилось {fmt_decimal(d.actual_percent)}%"
        )
    for d in stats.undersized:
        anomalies.append(
            f"объём по {d.symbol} после округления даёт риск {fmt_decimal(d.actual_percent)}% "
            f"— меньше половины заданных {fmt_decimal(d.target_percent)}%"
        )

    if len(stats.confirmed_drift_percents) >= PRICE_DRIFT_MIN_CARDS:
        avg_drift = sum(stats.confirmed_drift_percents, ZERO) / len(stats.confirmed_drift_percents)
        # "половина допустимого порога" — половина EXEC_MAX_PRICE_DRIFT_RATIO,
        # выраженного в процентах той же величины, что и сам price_drift_percent
        # (дрейф от опорной цены сигнала, раздел 5 ТЗ).
        allowed_half = max_price_drift_ratio * Decimal(100) / 2
        if avg_drift > allowed_half:
            anomalies.append(
                f"дрейф цены при подтверждении в среднем {fmt_decimal(avg_drift)}% "
                f"— выше половины допустимого порога"
            )

    total = stats.total_attempts
    if total >= GUARD_DOMINANCE_MIN_ATTEMPTS:
        for code, count in stats.refusals_by_code.items():
            if Decimal(count) > Decimal(total) * GUARD_DOMINANCE_RATIO:
                anomalies.append(
                    f"гвард {code} срабатывает подозрительно часто: {count} из {total} попыток"
                )

    return anomalies


def render_execution_digest(
    stats: ExecutionDigestStats, *, max_price_drift_ratio: Decimal
) -> str:
    """Раздел 12а ТЗ, макет сводки. Корректна и при stats.total_attempts == 0
    (нули вместо деления на ноль, средние строки просто не печатаются)."""
    anomalies = detect_anomalies(stats, max_price_drift_ratio=max_price_drift_ratio)

    lines = [
        "📊 <b>Исполнение за сутки</b>",
        "",
        f"Сигналов READY: {stats.ready_signals}",
        f"  показана карточка: {stats.total_cards}",
        f"    подтверждено: {stats.confirmed}",
        f"    отказ пользователя: {stats.declined}",
        f"    истекло по TTL: {stats.expired}",
        f"  отказ кода до карточки: {stats.total_refusals}",
    ]
    for code, count in sorted(stats.refusals_by_code.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {code} — {count}")

    has_averages = bool(
        stats.confirmed_risk_percents
        or stats.confirmed_risk_rewards
        or stats.confirmed_drift_percents
    )
    if has_averages:
        lines.append("")
    if stats.confirmed_risk_percents:
        avg_risk = sum(stats.confirmed_risk_percents, ZERO) / len(stats.confirmed_risk_percents)
        lines.append(
            f"Средний расчётный риск: {fmt_decimal(avg_risk)}% "
            f"(диапазон {fmt_decimal(min(stats.confirmed_risk_percents))}–"
            f"{fmt_decimal(max(stats.confirmed_risk_percents))}%)"
        )
    if stats.confirmed_risk_rewards:
        avg_rr = sum(stats.confirmed_risk_rewards, ZERO) / len(stats.confirmed_risk_rewards)
        lines.append(f"Средний RR: {fmt_decimal(avg_rr)}")
    if stats.confirmed_drift_percents:
        avg_drift = sum(stats.confirmed_drift_percents, ZERO) / len(stats.confirmed_drift_percents)
        lines.append(f"Средний дрейф цены на подтверждении: {fmt_decimal(avg_drift)}%")

    lines.append("")
    if anomalies:
        lines.append("Аномалии:")
        for a in anomalies:
            lines.append(f"  {a}")
    else:
        lines.append("Аномалии: нет")

    return "\n".join(lines)
