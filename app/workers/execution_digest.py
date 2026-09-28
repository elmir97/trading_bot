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
from datetime import UTC, datetime
from decimal import Decimal

from app.database.models.execution_order import ExecutionOrder
from app.database.models.reconciliation_event import ReconciliationEvent
from app.trading.enums import (
    ANOMALY_KINDS,
    ObservationStage,
    OrderStatus,
    ReconciliationKind,
    TradeSide,
)
from app.workers.base import fmt_decimal
from app.workers.scanner import ScanCycleStats

ZERO = Decimal(0)
# Ключ сортировки для события без created_at (ещё не записано в БД).
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

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

# Входы, у которых есть расчёт (объём, риск, RR, дрейф): подтверждённый
# сухой прогон и реальная отправка. Проскальзывание — только у реальных.
_ENTRY_STATUSES = (OrderStatus.DRY_RUN, OrderStatus.SUBMITTED, OrderStatus.FILLED)
_REAL_ENTRY_STATUSES = (OrderStatus.SUBMITTED, OrderStatus.FILLED)

SLIPPAGE_PRECISION = Decimal("0.001")


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
    # Отказ биржи на реальном входе — по коду BingX (error_code REJECTED-
    # строки, str(exc.code)), не по тексту ответа.
    rejected_by_code: dict[str, int] = field(default_factory=dict)
    unknown: int = 0
    pending: int = 0
    # Шаг 15.5.3: вход, исполнение которого подтверждено read-back.
    filled: int = 0
    # Шаг 15.5.3: стоп не подтверждён на бирже — (символ, сторона) по
    # строкам STOP_LOSS (ExecutionOrderRepository.list_unprotected_between).
    unprotected: list[tuple[str, str]] = field(default_factory=list)
    # Отказы гвардов по стадии evaluate(): "card" (карточки ещё нет) и
    # "confirm" (карточка показана, пришло «Да»). Строки без стадии (записаны
    # до её появления) — прежняя семантика «до карточки».
    refusals_card_by_code: dict[str, int] = field(default_factory=dict)
    refusals_confirm_by_code: dict[str, int] = field(default_factory=dict)
    # Сбои биржи (статус ERROR) по стадии и по классу исключения.
    errors_card: int = 0
    errors_confirm: int = 0
    errors_by_code: dict[str, int] = field(default_factory=dict)

    # Средние — по входам: подтверждённым сухим (DRY_RUN) и реальным
    # (SUBMITTED/FILLED). Раньше только по DRY_RUN — с EXEC_DRY_RUN=false
    # средние и правила по ним (отклонение риска, дрейф) не видели ни
    # одного настоящего входа. DECLINED/EXPIRED/REFUSED/ERROR/REJECTED/
    # UNKNOWN/PENDING — не входы, в средние не идут.
    entry_risk_percents: list[Decimal] = field(default_factory=list)
    entry_risk_rewards: list[Decimal] = field(default_factory=list)
    # 28.09: RR с комиссией — только у строк после миграции 19c5c0deedca.
    entry_risk_rewards_net: list[Decimal] = field(default_factory=list)
    entry_drift_percents: list[Decimal] = field(default_factory=list)
    # Проскальзывание исполнения: цена на «Да» (execution_orders.price) →
    # исполнение (trades.entry_price при fill_confirmed), в процентах, «+» —
    # в худшую сторону по направлению сделки. Только реальные входы.
    entry_slippage_percents: list[Decimal] = field(default_factory=list)

    # Шаг 15.6: события reconciler за окно — расхождения (в «Аномалии») и
    # факты: закрытия фактом биржи, разрешённые входы (строка сводки).
    reconciler_anomalies: dict[ReconciliationKind, int] = field(default_factory=dict)
    reconciler_facts: dict[ReconciliationKind, int] = field(default_factory=dict)
    # 28.09: события окна, уведомление о которых так и не доставлено (ещё в
    # переотправке или отказ) — и последнее из них (вид, символ).
    undelivered: int = 0
    last_undelivered: tuple[ReconciliationKind, str] | None = None

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
            + self.submitted + self.filled + self.rejected + self.unknown + self.pending
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
    unprotected: list[ExecutionOrder] | None = None,
    reconciler_events: list[ReconciliationEvent] | None = None,
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
    stats.unprotected = [
        (row.symbol, row.position_side.value) for row in (unprotected or [])
    ]
    for event in reconciler_events or []:
        bucket = (
            stats.reconciler_anomalies if event.kind in ANOMALY_KINDS else stats.reconciler_facts
        )
        bucket[event.kind] = bucket.get(event.kind, 0) + 1
    undelivered = sorted(
        (e for e in reconciler_events or [] if e.notified_at is None),
        key=lambda e: (e.created_at or _EPOCH, e.id or 0),
    )
    stats.undelivered = len(undelivered)
    if undelivered:
        stats.last_undelivered = (undelivered[-1].kind, undelivered[-1].symbol)

    for row in rows:
        if row.status in _ENTRY_STATUSES:
            _collect_entry_numbers(stats, row, target_risk_percent)
        if row.status in _REAL_ENTRY_STATUSES:
            slippage = _execution_slippage_percent(row)
            if slippage is not None:
                stats.entry_slippage_percents.append(slippage)

        if row.status is OrderStatus.DRY_RUN:
            stats.confirmed += 1
        elif row.status is OrderStatus.SUBMITTED:
            stats.submitted += 1
        elif row.status is OrderStatus.FILLED:
            stats.filled += 1
        elif row.status is OrderStatus.REJECTED:
            stats.rejected += 1
            code = row.error_code or "?"
            stats.rejected_by_code[code] = stats.rejected_by_code.get(code, 0) + 1
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


def _collect_entry_numbers(
    stats: ExecutionDigestStats, row: ExecutionOrder, target_risk_percent: Decimal | None
) -> None:
    if row.risk_percent is not None:
        stats.entry_risk_percents.append(row.risk_percent)
        _check_risk_deviation(stats, row, target_risk_percent)
    if row.risk_reward is not None:
        stats.entry_risk_rewards.append(row.risk_reward)
    if row.risk_reward_net is not None:
        stats.entry_risk_rewards_net.append(row.risk_reward_net)
    if row.price_drift_percent is not None:
        stats.entry_drift_percents.append(row.price_drift_percent)


def _execution_slippage_percent(row: ExecutionOrder) -> Decimal | None:
    """Цена на «Да» (row.price) → исполнение (trades.entry_price), «+» — в
    худшую сторону: LONG купил дороже, SHORT продал дешевле. None — нет
    сделки или исполнение не подтверждено: у предварительной сделки
    entry_price плановая, проскальзывание из неё выдумано. row.trade
    загружен заранее (ExecutionOrderRepository.list_entries_between)."""
    trade = row.trade
    if trade is None or not trade.fill_confirmed or trade.entry_price is None:
        return None
    if row.price is None or row.price <= ZERO:
        return None
    diff = trade.entry_price - row.price
    signed = diff if row.position_side is TradeSide.LONG else -diff
    return signed / row.price * Decimal(100)


_RECONCILER_LABELS = {
    ReconciliationKind.ORPHAN_POSITION: "позиция без сделки",
    ReconciliationKind.QUANTITY_MISMATCH: "объём не сходится",
    ReconciliationKind.STOP_MISSING: "позиция без стопа",
    ReconciliationKind.AMBIGUOUS: "неоднозначно",
    ReconciliationKind.PNL_MISMATCH: "PnL не сходится с биржей",
    ReconciliationKind.CLOSED_STOP_LOSS: "закрыто по стопу",
    ReconciliationKind.CLOSED_TAKE_PROFIT: "по тейку",
    ReconciliationKind.CLOSED_OUTSIDE_BOT: "вне бота",
    ReconciliationKind.PARTIAL_CLOSE: "частично",
    ReconciliationKind.ENTRY_CONFIRMED: "вход найден",
    ReconciliationKind.ENTRY_NOT_PLACED: "вход не выставлен",
}


def _by_kind(counts: dict[ReconciliationKind, int]) -> str:
    return ", ".join(
        f"{_RECONCILER_LABELS[kind]} — {count}"
        for kind, count in sorted(counts.items(), key=lambda kv: -kv[1])
    )


def _signed_percent(value: Decimal) -> str:
    text = fmt_decimal(value.quantize(SLIPPAGE_PRECISION))
    return f"+{text}" if value > ZERO else text


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

    # Шаг 15.5.3: первой — это единственная аномалия, при которой деньги
    # под риском прямо сейчас.
    counts: dict[tuple[str, str], int] = {}
    for key in stats.unprotected:
        counts[key] = counts.get(key, 0) + 1
    for (symbol, side), count in counts.items():
        anomalies.append(f"позиция без стопа: {symbol} {side} — {count}")

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

    if len(stats.entry_drift_percents) >= PRICE_DRIFT_MIN_CARDS:
        avg_drift = sum(stats.entry_drift_percents, ZERO) / len(stats.entry_drift_percents)
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

    # Шаг 15.6: расхождения reconciler — журнал не правился, человек должен
    # посмотреть сам. От одного случая.
    if stats.reconciler_anomalies:
        total = sum(stats.reconciler_anomalies.values())
        anomalies.append(
            f"сверка с биржей: расхождений {total} ({_by_kind(stats.reconciler_anomalies)})"
        )

    # 28.09: уведомление сверки так и не ушло — человек мог не узнать о
    # закрытии или тревоге. От одного случая.
    if stats.undelivered and stats.last_undelivered is not None:
        kind, symbol = stats.last_undelivered
        anomalies.append(
            f"уведомления сверки не доставлены — {stats.undelivered} "
            f"(последнее: {_RECONCILER_LABELS[kind]}, {symbol})"
        )

    # Реальный вход, отклонённый биржей, — от одного случая: сигнал был,
    # «Да» было, позиции нет. Код, а не текст — текст BingX меняется и
    # переводится, код — нет.
    if stats.rejected:
        by_code = ", ".join(
            f"код {code} — {count}"
            for code, count in sorted(stats.rejected_by_code.items(), key=lambda kv: -kv[1])
        )
        anomalies.append(f"биржа отклонила вход: {stats.rejected} ({by_code})")

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
        f"    исполнено (read-back): {stats.filled}",
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
        stats.entry_risk_percents
        or stats.entry_risk_rewards
        or stats.entry_drift_percents
        or stats.entry_slippage_percents
    )
    if has_averages:
        lines.append("")
    if stats.entry_risk_percents:
        avg_risk = sum(stats.entry_risk_percents, ZERO) / len(stats.entry_risk_percents)
        lines.append(
            f"Средний расчётный риск: {fmt_decimal(avg_risk)}% "
            f"(диапазон {fmt_decimal(min(stats.entry_risk_percents))}–"
            f"{fmt_decimal(max(stats.entry_risk_percents))}%)"
        )
    if stats.entry_risk_rewards:
        avg_rr = sum(stats.entry_risk_rewards, ZERO) / len(stats.entry_risk_rewards)
        net = stats.entry_risk_rewards_net
        net_note = (
            f" · с комиссией {fmt_decimal(sum(net, ZERO) / len(net))}" if net else ""
        )
        lines.append(f"Средний RR: {fmt_decimal(avg_rr)}{net_note}")
    if stats.entry_drift_percents:
        avg_drift = sum(stats.entry_drift_percents, ZERO) / len(stats.entry_drift_percents)
        lines.append(
            f"Средний дрейф цены сигнала к моменту подтверждения: {fmt_decimal(avg_drift)}%"
        )
    if stats.entry_slippage_percents:
        slippages = stats.entry_slippage_percents
        avg_slippage = sum(slippages, ZERO) / len(slippages)
        lines.append(
            f"Проскальзывание исполнения (на «Да» → исполнение): среднее "
            f"{_signed_percent(avg_slippage)}%, худшее {_signed_percent(max(slippages))}% "
            f"— входов {len(slippages)}, «+» — в худшую сторону"
        )

    if stats.reconciler_facts:
        lines.append("")
        lines.append(f"Сверка с биржей: {_by_kind(stats.reconciler_facts)}")

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
