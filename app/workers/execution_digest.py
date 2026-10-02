"""Ежедневная сводка исполнения (этап 15.4, раздел 12а ТЗ) — сверка с биржей.

02.10.2026: вход по сигналу удалён, вместе с ним из сводки ушли воронка
карточек, средние по входам и их аномалии. Осталось то, что пишет reconciler
(reconciliation_events): факты закрытия, расхождения, недоставленные
уведомления, и пульс самого reconciler. Действия с позициями (этап 4)
добавятся сюда отдельным разделом.

build_stats() и render_execution_digest() — чистые функции без I/O,
проверяются тестами без БД (tests/test_execution_digest.py); DailyJobs
(app/workers/daily.py) только достаёт события за окно и вызывает их.

Окно — скользящие 24 часа до момента отправки, не календарные сутки:
отправка (EXEC_DAILY_DIGEST_HOUR) почти никогда не совпадает с локальной
полночью, и календарные сутки резали бы события между часом отправки и
полночью. Само окно считает DailyJobs (window_start = now - 24h).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.database.models.position_action import PositionAction
from app.database.models.reconciliation_event import ReconciliationEvent
from app.trading.enums import ANOMALY_KINDS, PositionActionStatus, ReconciliationKind

if TYPE_CHECKING:
    # Только тип: сводке не нужен весь reconciler (биржа, журнал) при импорте.
    from app.workers.reconciler import ReconcilerWindow

# Ключ сортировки для события без created_at (ещё не записано в БД).
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class ExecutionDigestStats:
    """Итог за окно сводки — только числа, без форматирования."""

    # Шаг 15.6: события reconciler за окно — расхождения (в «Аномалии») и
    # факты: закрытия фактом биржи, разрешённые входы (строка сводки).
    reconciler_anomalies: dict[ReconciliationKind, int] = field(default_factory=dict)
    reconciler_facts: dict[ReconciliationKind, int] = field(default_factory=dict)
    # 28.09: события окна, уведомление о которых так и не доставлено (ещё в
    # переотправке или отказ) — и последнее из них (вид, символ).
    undelivered: int = 0
    last_undelivered: tuple[ReconciliationKind, str] | None = None
    # Этап 4: карточки действий с позицией за окно — по статусу.
    actions: dict[PositionActionStatus, int] = field(default_factory=dict)


def build_stats(
    reconciler_events: list[ReconciliationEvent],
    actions: list[PositionAction] | None = None,
) -> ExecutionDigestStats:
    """reconciler_events — события reconciliation_events одного пользователя
    за окно сводки (ReconciliationEventRepository.list_between); actions —
    карточки действий с позицией за то же окно (этап 4)."""
    stats = ExecutionDigestStats()
    for action in actions or []:
        stats.actions[action.status] = stats.actions.get(action.status, 0) + 1
    for event in reconciler_events:
        bucket = (
            stats.reconciler_anomalies if event.kind in ANOMALY_KINDS else stats.reconciler_facts
        )
        bucket[event.kind] = bucket.get(event.kind, 0) + 1
    undelivered = sorted(
        (e for e in reconciler_events if e.notified_at is None),
        key=lambda e: (e.created_at or _EPOCH, e.id or 0),
    )
    stats.undelivered = len(undelivered)
    if undelivered:
        stats.last_undelivered = (undelivered[-1].kind, undelivered[-1].symbol)
    return stats


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
    ReconciliationKind.STOP_RESCUE_FAILED: "стоп не выставлен при входе",
    ReconciliationKind.STOP_UNVERIFIED: "стоп не подтверждён при входе",
    ReconciliationKind.LIQUIDATION_BEFORE_STOP: "ликвидация раньше стопа",
    ReconciliationKind.ENTRY_PAST_STOP: "вход за уровнем стопа",
}


def _by_kind(counts: dict[ReconciliationKind, int]) -> str:
    return ", ".join(
        f"{_RECONCILER_LABELS[kind]} — {count}"
        for kind, count in sorted(counts.items(), key=lambda kv: -kv[1])
    )


def _by_kind(counts: dict[ReconciliationKind, int]) -> str:
    return ", ".join(
        f"{_RECONCILER_LABELS[kind]} — {count}"
        for kind, count in sorted(counts.items(), key=lambda kv: -kv[1])
    )


_ACTION_LABELS = {
    PositionActionStatus.DONE: "выполнено",
    PositionActionStatus.DRY_RUN: "сухой прогон",
    PositionActionStatus.FAILED: "сбой",
    PositionActionStatus.REFUSED: "отказ",
    PositionActionStatus.DECLINED: "отменено",
    PositionActionStatus.EXPIRED: "устарело",
    PositionActionStatus.SUBMITTED: "в процессе",
    PositionActionStatus.CARD: "без решения",
}


def _actions_line(counts: dict[PositionActionStatus, int]) -> str:
    total = sum(counts.values())
    parts = ", ".join(
        f"{_ACTION_LABELS[status]} {counts[status]}"
        for status in _ACTION_LABELS if counts.get(status)
    )
    return f"карточек {total} ({parts})"


def detect_anomalies(stats: ExecutionDigestStats) -> list[str]:
    """Пункты «Аномалий» — то, что человек глазами бы не поймал."""
    anomalies: list[str] = []

    # Шаг 15.6: расхождения reconciler — журнал не правился, человек должен
    # посмотреть сам. От одного случая.
    if stats.reconciler_anomalies:
        total = sum(stats.reconciler_anomalies.values())
        anomalies.append(
            f"сверка с биржей: расхождений {total} ({_by_kind(stats.reconciler_anomalies)})"
        )

    # Этап 4: действие не выполнено после «Да» (биржа отказала, read-back не
    # подтвердил, старый стоп не снялся) — человек должен посмотреть сам.
    failed = stats.actions.get(PositionActionStatus.FAILED, 0)
    if failed:
        anomalies.append(f"действия с позициями не выполнены — {failed}")

    # 28.09: уведомление сверки так и не ушло — человек мог не узнать о
    # закрытии или тревоге. От одного случая.
    if stats.undelivered and stats.last_undelivered is not None:
        kind, symbol = stats.last_undelivered
        anomalies.append(
            f"уведомления сверки не доставлены — {stats.undelivered} "
            f"(последнее: {_RECONCILER_LABELS[kind]}, {symbol})"
        )

    return anomalies


def render_reconciler_line(window: ReconcilerWindow, tz_offset_hours: int) -> str:
    """28.09: пульс reconciler в сводке — «Сверка: циклов N, последний
    HH:MM, ошибок E»; после рестарта внутри окна — «Сверка (с HH:MM): …».
    Время — в поясе пользователя."""

    def local(moment: datetime) -> str:
        return f"{moment + timedelta(hours=tz_offset_hours):%H:%M}"

    head = f"Сверка (с {local(window.started_at)})" if window.since_start else "Сверка"
    last = local(window.last_cycle_at) if window.last_cycle_at is not None else "—"
    return f"{head}: циклов {window.cycles}, последний {last}, ошибок {window.errors}"


def render_execution_digest(
    stats: ExecutionDigestStats,
    *,
    reconciler: ReconcilerWindow | None = None,
    tz_offset_hours: int = 0,
) -> str:
    """Сводка за окно. Пустой день тоже рендерится: «Аномалии: нет» — уже
    информация (раздел 12а: «пустая строка тут не годится»)."""
    anomalies = detect_anomalies(stats)

    lines = ["📊 <b>Исполнение за последние 24 часа</b>", ""]
    if stats.reconciler_facts:
        lines.append(f"Сверка с биржей: {_by_kind(stats.reconciler_facts)}")
    else:
        lines.append("Сверка с биржей: событий нет")

    if stats.actions:
        lines.append(f"Действия с позициями: {_actions_line(stats.actions)}")

    lines.append("")
    if anomalies:
        lines.append("Аномалии:")
        for a in anomalies:
            lines.append(f"  {a}")
    else:
        lines.append("Аномалии: нет")

    if reconciler is not None:
        lines.append("")
        lines.append(render_reconciler_line(reconciler, tz_offset_hours))

    return "\n".join(lines)
