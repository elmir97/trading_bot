"""Переотправка недоставленных уведомлений сверки — решения без I/O (28.09).

Критичные уведомления (закрытия фактом биржи, расхождения, тревоги
read-back) — «хотя бы один раз»: событие в reconciliation_events живёт с
notified_at IS NULL, пока Telegram не принял сообщение, и reconciler
переотправляет его по расписанию:

- первые REDELIVERY_FAST (30 мин) от created_at — каждый цикл reconciler;
- дальше — не чаще раза в REDELIVERY_SLOW_INTERVAL (10 мин); первый цикл
  после быстрого окна — попытка всегда, и на ней один ERROR в лог;
- старше max_age (Settings.reconciler_notify_max_age_hours, 24 ч) — отказ:
  gave_up_at, WARNING, больше не шлём;
- бот заблокирован (Delivery.FORBIDDEN) — тоже gave_up_at, без повторов.

Дубль допустим, потеря — нет. Отправка, запись и логи —
app/workers/reconciler.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Расписание (решение 28.09). Обрывы Telegram на проде — до 10 мин (22.09
# 03:05–03:15): быстрое окно их перекрывает втрое.
REDELIVERY_FAST = timedelta(minutes=30)
REDELIVERY_SLOW_INTERVAL = timedelta(minutes=10)

# Сообщение, ушедшее позже этого, получает первой строкой «⏱ Событие от
# HH:MM (доставлено с опозданием)» — иначе старое закрытие читается как
# только что случившееся.
LATE_NOTICE_AFTER = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class Redelivery:
    """due — отправлять на этом цикле; expired — отказ по возрасту;
    entering_slow — первая попытка после быстрого окна (один ERROR)."""

    due: bool
    expired: bool = False
    entering_slow: bool = False


def redelivery_due(
    *,
    created_at: datetime,
    last_attempt_at: datetime | None,
    now: datetime,
    fast: timedelta,
    slow_interval: timedelta,
    max_age: timedelta,
) -> Redelivery:
    age = now - created_at
    if age > max_age:
        return Redelivery(due=False, expired=True)
    if age <= fast:
        return Redelivery(due=True)
    fast_end = created_at + fast
    if last_attempt_at is None or last_attempt_at <= fast_end:
        # Первый цикл после быстрого окна: попытка сразу, чтобы last_attempt_at
        # ушёл за границу — иначе ERROR «перехожу на редкий режим» писался бы
        # каждый цикл до следующей попытки.
        return Redelivery(due=True, entering_slow=True)
    return Redelivery(due=now - last_attempt_at >= slow_interval)


def late_notice(*, created_at: datetime, now: datetime, tz_offset_hours: int) -> str | None:
    """Первая строка переотправки, если событие старше LATE_NOTICE_AFTER.
    Время — в часовом поясе пользователя."""
    if now - created_at <= LATE_NOTICE_AFTER:
        return None
    local = created_at + timedelta(hours=tz_offset_hours)
    return f"⏱ Событие от {local:%H:%M} (доставлено с опозданием)"
