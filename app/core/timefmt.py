"""Время в уведомлениях — в часовом поясе пользователя.

Общая точка (29.09): строка «Закрыта: DD.MM HH:MM» всех уведомлений reconciler
о закрытии — стоп/тейк бота, вне бота, частичное (Reconciler._close_text).
Сдвиг пояса — app.trading.risk.tz_offset_for.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def fmt_local_datetime(value: datetime, tz_offset_hours: int) -> str:
    """UTC-время → «DD.MM HH:MM» в поясе пользователя."""
    return f"{value + timedelta(hours=tz_offset_hours):%d.%m %H:%M}"


def closed_at_line(value: datetime, tz_offset_hours: int) -> str:
    """Строка уведомления о закрытии: время исполнения ордера биржи, а не
    момент, когда сверка его нашла, — уведомление может прийти позже."""
    return f"Закрыта: {fmt_local_datetime(value, tz_offset_hours)}"
