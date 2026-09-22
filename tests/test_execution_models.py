"""Тесты app/execution/models.py::client_order_id (раздел 16 ТЗ, шаг 15.5.2).

Юнит, без БД — чистая функция. Тесты нового формата (разделитель "u",
role.letter вместо role.value) — не замки: на прежнем формате
f"tj{signal_id}{user_id}{role.value}" test_disambiguates_swapped_signal_
and_user_id падал бы (12,3) и (1,23) давали одну строку "tj123ENTRY"),
test_ascii_alnum_only падал бы на STOP_LOSS/TAKE_PROFIT (подчёркивание),
test_max_length_on_int32_ids падал бы (33 символа против лимита 24).
"""

from __future__ import annotations

import re

import pytest

from app.execution.models import client_order_id
from app.trading.enums import OrderRole

# Integer-колонка (не BigInteger) — максимум 2**31-1, см. IntPKMixin
# (app/database/base.py) и signals.id/users.id.
_INT32_MAX = 2_147_483_647

_ASCII_ALNUM = re.compile(r"^[A-Za-z0-9]+$")


@pytest.mark.parametrize("role", list(OrderRole))
def test_ascii_alnum_only(role: OrderRole) -> None:
    result = client_order_id(signal_id=12, user_id=3, role=role)
    assert _ASCII_ALNUM.match(result), result


@pytest.mark.parametrize("role", list(OrderRole))
def test_max_length_on_int32_ids(role: OrderRole) -> None:
    result = client_order_id(signal_id=_INT32_MAX, user_id=_INT32_MAX, role=role)
    assert len(result) <= 24, (len(result), result)
    # Тот же лимит, что уже жёстко проверяет BingXClient.place_market_order
    # (1-40 символов, раздел 16 ТЗ) — с запасом, не впритык.
    assert len(result) <= 40


def test_disambiguates_swapped_signal_and_user_id() -> None:
    """Без разделителя (signal_id=12, user_id=3) и (signal_id=1, user_id=23)
    давали бы одну и ту же строку "tj123E" — а UNIQUE на client_order_id
    глобальный по всей таблице execution_orders, не по паре id."""
    a = client_order_id(signal_id=12, user_id=3, role=OrderRole.ENTRY)
    b = client_order_id(signal_id=1, user_id=23, role=OrderRole.ENTRY)
    assert a != b


def test_deterministic() -> None:
    a = client_order_id(signal_id=42, user_id=7, role=OrderRole.STOP_LOSS)
    b = client_order_id(signal_id=42, user_id=7, role=OrderRole.STOP_LOSS)
    assert a == b


def test_roles_produce_distinct_ids() -> None:
    ids = {
        client_order_id(signal_id=1, user_id=1, role=role) for role in OrderRole
    }
    assert len(ids) == len(list(OrderRole))
