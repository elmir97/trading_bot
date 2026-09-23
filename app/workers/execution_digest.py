"""Ежедневная сводка исполнения (этап 15.4, раздел 12а ТЗ).

Наблюдение за этапом ведёт код, а не человек глазами: карточка
подтверждения (app/bot/handlers/execution.py) и ExecutionService.evaluate()
(app/execution/service.py) уже пишут по одной строке в execution_orders
на каждый исход попытки входа по READY-сигналу —

    DRY_RUN   — подтверждено ("Да", прошло все guard-ы)
    DECLINED  — пользователь нажал "Нет"
    EXPIRED   — карточка прожила 60 секунд без ответа
    REFUSED   — отказал guard (error_code — какой): на построении карточки
                (stage=card, карточки нет) или на «Да» (stage=confirm, карточка
                уже была показана)
    ERROR     — сбой обращения к бирже на пути входа (error_code — класс
                исключения), тоже на одной из двух стадий

Этот модуль только читает эти строки и считает: отдельной таблицы под
статистику нет и не заводится — дублировать то, что уже пишется в
execution_orders, значит рано или поздно разойтись с ним. build_stats() и
render_execution_digest() — чистые функции без I/O, поэтому проверяются
тестами без БД (tests/test_execution_digest.py); DailyJobs (app/workers/daily.py)
только достаёт строки за окно сводки и вызывает их.

Окно — скользящие 24 часа до момента отправки, не календарные сутки:
отправка (EXEC_DAILY_DIGEST_HOUR) почти никогда не совпадает с локальной
полночью, и календарные сутки резали бы события между часом отправки и
полночью — они не попадали бы ни в сегодняшнюю сводку, ни в завтрашнюю.
Само окно считает DailyJobs (window_start = now - 24h), этому модулю
известны только уже готовые rows/ready_signals.

"Сигналов READY" в шапке сводки — исключение: это count() из
signal_notifications (SignalNotificationRepository.count_ready_between()),
не из execution_orders, потому что отказ гварда происходит до появления
карточки, а карточка (и, значит, execution_orders-строка) при этом ещё не
существует — без отдельного источника READY-сигналы, упёршиеся в гвард,
были бы не видны. Шаг 15.5.2а: считаются отправленные уведомления
(события), а не слоты — слот, уведомивший дважды за сутки, даёт два.
build_stats() принимает готовое число ready_signals параметром, I/O
остаётся в DailyJobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.database.models.execution_order import ExecutionOrder
from app.trading.enums import ObservationStage, OrderStatus
from app.workers.base import fmt_decimal
from app.workers.scanner import ScanCycleStats

ZERO = Decimal(0)

# "расчётный риск отклонился от заданного больше чем на 10%" (раздел 12а).
RISK_DEVIATION_RATIO = Decimal("0.10")
# "объём после округления даёт риск меньше половины заданного" — отдельная,
# более серьёзная планка внутри той же деформации от округления лота вниз.
RISK_UNDERSIZED_RATIO = Decimal("0.50")
# "гвард срабатывает подозрительно часто (более половины сигналов)" — доля
# от ВСЕХ попыток в окне сводки (показанные карточки + отказы гвардов), не
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
    """Итог за окно сводки — только числа, без форматирования (раздел 12а).

    Окно — скользящие 24 часа до отправки (см. докстринг модуля), не
    календарные сутки."""

    # Из signal_notifications (SignalNotificationRepository.count_ready_between),
    # не из execution_orders — сколько раз READY-сетап реально дошёл до
    # пользователя уведомлением, независимо от того, нажималась ли кнопка
    # и прошла ли попытка гвардов (раздел 12а).
    ready_signals: int = 0

    confirmed: int = 0
    declined: int = 0
    expired: int = 0
    # Шаг 15.5.2а: исходы реальной отправки (EXEC_DRY_RUN=false) — тоже
    # подтверждённые карточки, но с ответом биржи вместо сухого прогона.
    # PENDING в окне сводки значит «ответа не дождались» — по разделу 8 ТЗ
    # такая строка разбирается так же, как UNKNOWN.
    submitted: int = 0
    rejected: int = 0
    unknown: int = 0
    pending: int = 0
    # Отказы гвардов по стадии evaluate(): "card" (карточки ещё нет) и
    # "confirm" (карточка показана, пришло «Да»). Строки без стадии (записаны
    # до её появления) — прежняя семантика «до карточки».
    refusals_card_by_code: dict[str, int] = field(default_factory=dict)
    refusals_confirm_by_code: dict[str, int] = field(default_factory=dict)
    # Сбои биржи (статус ERROR) по стадии и по классу исключения.
    errors_card: int = 0
    errors_confirm: int = 0
    errors_by_code: dict[str, int] = field(default_factory=dict)

    # Только у подтверждённых (DRY_RUN) есть реальный объём/маржа/RR —
    # только по ним считаются "средние" в сводке.
    confirmed_risk_percents: list[Decimal] = field(default_factory=list)
    confirmed_risk_rewards: list[Decimal] = field(default_factory=list)
    confirmed_drift_percents: list[Decimal] = field(default_factory=list)

    risk_deviations: list[RiskDeviation] = field(default_factory=list)
    undersized: list[RiskDeviation] = field(default_factory=list)

    @property
    def refusals_by_code(self) -> dict[str, int]:
        """Отказы гвардов по коду за обе стадии — для правила «гвард
        срабатывает подозрительно часто»: один гвард не должен прятаться от
        правила за разбивкой на стадии."""
        merged = dict(self.refusals_card_by_code)
        for code, count in self.refusals_confirm_by_code.items():
            merged[code] = merged.get(code, 0) + count
        return merged

    @property
    def refused_card(self) -> int:
        return sum(self.refusals_card_by_code.values())

    @property
    def refused_confirm(self) -> int:
        return sum(self.refusals_confirm_by_code.values())

    @property
    def total_refusals(self) -> int:
        return self.refused_card + self.refused_confirm

    @property
    def total_errors(self) -> int:
        return self.errors_card + self.errors_confirm

    @property
    def total_cards(self) -> int:
        """Показанных карточек. Кроме трёх исходов самой карточки сюда входят
        попытки, оборвавшиеся на «Да» (отказ гварда или сбой биржи на втором
        вызове evaluate()): карточка при них уже была показана. Отказы и сбои
        на построении карточки не входят — карточка при них не рисуется
        (раздел 7 ТЗ)."""
        return (
            self.confirmed + self.declined + self.expired
            + self.submitted + self.rejected + self.unknown + self.pending
            + self.refused_confirm + self.errors_confirm
        )

    @property
    def total_attempts(self) -> int:
        """Одна строка = одна попытка входа: карточки + отказы и сбои до
        карточки."""
        return self.total_cards + self.refused_card + self.errors_card

    @property
    def guard_attempts(self) -> int:
        """Попытки, дошедшие до гвардов — знаменатель правила «гвард
        срабатывает подозрительно часто». Сбои биржи не входят: сбой в
        for_user случается до гвардов, и с ними в знаменателе правило
        замолкало бы как раз во время сбоя биржи."""
        return self.total_attempts - self.total_errors


def build_stats(
    rows: list[ExecutionOrder],
    *,
    target_risk_percent: Decimal | None,
    ready_signals: int = 0,
) -> ExecutionDigestStats:
    """rows — строки execution_orders (role=ENTRY) за окно сводки одного
    пользователя (скользящие 24 часа, не календарные сутки — см. докстринг
    модуля), см. ExecutionOrderRepository.list_entries_between().
    target_risk_percent — текущий risk_per_trade_percent торгового плана,
    точка отсчёта для "риск отклонился от заданного" (раздел 12а).
    ready_signals — SignalNotificationRepository.count_ready_between() за то
    же окно: считается отдельно от rows, источник другой
    (signal_notifications, не execution_orders), поэтому передаётся готовым
    числом, а не строками."""
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
        elif row.status is OrderStatus.SUBMITTED:
            stats.submitted += 1
        elif row.status is OrderStatus.REJECTED:
            stats.rejected += 1
        elif row.status is OrderStatus.UNKNOWN:
            stats.unknown += 1
        elif row.status is OrderStatus.PENDING:
            stats.pending += 1
        elif row.status is OrderStatus.DECLINED:
            stats.declined += 1
        elif row.status is OrderStatus.EXPIRED:
            stats.expired += 1
        elif row.status is OrderStatus.REFUSED:
            code = row.error_code or "?"
            # Только явный "confirm" — всё остальное (NULL у старых строк,
            # "card") это «до карточки». Эвристик по коду нет: окно сводки —
            # скользящие 24 часа, старые строки быстро уходят из него.
            by_code = (
                stats.refusals_confirm_by_code
                if row.stage == ObservationStage.CONFIRM
                else stats.refusals_card_by_code
            )
            by_code[code] = by_code.get(code, 0) + 1
        elif row.status is OrderStatus.ERROR:
            if row.stage == ObservationStage.CONFIRM:
                stats.errors_confirm += 1
            else:
                stats.errors_card += 1
            code = row.error_code or "?"
            stats.errors_by_code[code] = stats.errors_by_code.get(code, 0) + 1

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
                f"дрейф цены сигнала к моменту подтверждения в среднем "
                f"{fmt_decimal(avg_drift)}% — выше половины порога гварда PRICE_DRIFT"
            )

    total = stats.guard_attempts
    if total >= GUARD_DOMINANCE_MIN_ATTEMPTS:
        for code, count in stats.refusals_by_code.items():
            if Decimal(count) > Decimal(total) * GUARD_DOMINANCE_RATIO:
                anomalies.append(
                    f"гвард {code} срабатывает подозрительно часто: {count} из {total} попыток"
                )

    if stats.unknown or stats.pending:
        anomalies.append(
            f"исход отправки неизвестен: {stats.unknown + stats.pending} "
            f"(UNKNOWN — {stats.unknown}, PENDING — {stats.pending}) — сверить позиции в BingX"
        )

    if stats.total_errors:
        by_class = ", ".join(
            f"{code} — {count}"
            for code, count in sorted(stats.errors_by_code.items(), key=lambda kv: -kv[1])
        )
        anomalies.append(
            f"сбои биржи при попытках входа: {stats.total_errors} из "
            f"{stats.total_attempts} ({by_class})"
        )

    return anomalies


def _append_codes(lines: list[str], by_code: dict[str, int], *, indent: int) -> None:
    """Разбивка по кодам — только ненулевая: строки самой воронки печатаются
    всегда (человек должен видеть, что проверка была), а нули по кодам —
    шум."""
    for code, count in sorted(by_code.items(), key=lambda kv: -kv[1]):
        if count:
            lines.append(f"{' ' * indent}{code} — {count}")


def render_execution_digest(
    stats: ExecutionDigestStats,
    *,
    max_price_drift_ratio: Decimal,
    scan_cycle: ScanCycleStats | None = None,
) -> str:
    """Раздел 12а ТЗ, макет сводки. Корректна и при stats.total_attempts == 0
    (нули вместо деления на ноль, средние строки просто не печатаются).

    scan_cycle — последний замер SetupScanner.run() (раздел "троттлинг
    сканера"), чтобы расширение списка символов было измеримым, а не на
    глаз. None, если сканер ни разу не отработал после старта процесса —
    это не то же самое, что "0 запросов", строка просто не печатается."""
    anomalies = detect_anomalies(stats, max_price_drift_ratio=max_price_drift_ratio)

    lines = [
        "📊 <b>Исполнение за последние 24 часа</b>",
        "",
        f"Сигналов READY: {stats.ready_signals}",
        f"  показана карточка: {stats.total_cards}",
        f"    подтверждено: {stats.confirmed}",
        f"    отправлено на биржу: {stats.submitted}",
        f"    отклонено биржей: {stats.rejected}",
        f"    исход неизвестен: {stats.unknown}",
        f"    без ответа биржи (PENDING): {stats.pending}",
        f"    отказ пользователя: {stats.declined}",
        f"    истекло по TTL: {stats.expired}",
        f"    отказ кода при подтверждении: {stats.refused_confirm}",
    ]
    _append_codes(lines, stats.refusals_confirm_by_code, indent=6)
    lines.append(f"    сбой биржи при подтверждении: {stats.errors_confirm}")
    lines.append(f"  отказ кода до карточки: {stats.refused_card}")
    _append_codes(lines, stats.refusals_card_by_code, indent=4)
    lines.append(f"  сбой биржи до карточки: {stats.errors_card}")

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
        lines.append(
            f"Средний дрейф цены сигнала к моменту подтверждения: {fmt_decimal(avg_drift)}%"
        )

    lines.append("")
    if anomalies:
        lines.append("Аномалии:")
        for a in anomalies:
            lines.append(f"  {a}")
    else:
        lines.append("Аномалии: нет")

    if scan_cycle is not None:
        lines.append("")
        lines.append(
            f"Скан рынка: {scan_cycle.symbols_scanned} символов, "
            f"{scan_cycle.requests_made} запросов, "
            f"{fmt_decimal(Decimal(str(round(scan_cycle.duration_seconds, 1))))} с"
        )

    return "\n".join(lines)
