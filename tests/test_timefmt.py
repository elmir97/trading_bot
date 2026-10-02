"""Время в уведомлениях (app/core/timefmt.py) и форматтеры сумм в
app/core/numfmt.py — 29.09, уведомление о закрытии вне бота."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core import numfmt
from app.core.timefmt import closed_at_line, fmt_local_datetime


def test_local_datetime_in_user_timezone() -> None:
    """LINK #3: исполнение 04:03:24 UTC — 09:03 по Екб (+5)."""
    assert fmt_local_datetime(datetime(2026, 9, 29, 4, 3, 24, tzinfo=UTC), 5) == "29.09 09:03"


def test_local_datetime_crosses_midnight() -> None:
    assert fmt_local_datetime(datetime(2026, 9, 29, 20, 30, tzinfo=UTC), 5) == "30.09 01:30"


def test_closed_at_line() -> None:
    at = datetime(2026, 9, 29, 4, 3, 24, tzinfo=UTC)
    assert closed_at_line(at, 5) == "Закрыта: 29.09 09:03"


def test_money_formatters_live_in_core_and_bot_reexports() -> None:
    """Воркеры не импортируют app.bot — fmt_qty/fmt_money/fmt_amount в core,
    app.bot.formatting отдаёт те же объекты."""
    from app.bot import formatting

    for name in ("fmt_qty", "fmt_money", "fmt_amount"):
        assert getattr(formatting, name) is getattr(numfmt, name)
    assert numfmt.fmt_money(Decimal("736.484953")) == "+736.48"
    assert numfmt.fmt_amount(Decimal("15.055386")) == "15.06"
    assert numfmt.fmt_qty(Decimal("2037.800000000000")) == "2037.8"


@pytest.mark.parametrize(
    ("value", "money", "amount"),
    [
        ("-0.004", "0.00", "0.00"),     # «PnL -0.00» на экране «Позиции», 02.10
        ("0.004", "0.00", "0.00"),
        ("0", "0.00", "0.00"),
        ("-0.005", "-0.01", "-0.01"),
        ("0.005", "+0.01", "0.01"),
    ],
)
def test_zero_after_rounding_has_no_sign(value: str, money: str, amount: str) -> None:
    assert numfmt.fmt_money(Decimal(value)) == money
    assert numfmt.fmt_amount(Decimal(value)) == amount
