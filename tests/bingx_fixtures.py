"""Живые ответы демо BingX, снятые GET 27.09.2026 (tests/fixtures/
bingx_demo_20260927.json). Элемент фикстуры хранит ключи ответа и значения
полей по allowlist; здесь из него собирается сырой dict, как его отдаёт
биржа, — ровно те поля, что были сняты, без выдуманных."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures" / "bingx_demo_20260927.json"
# 29.09: LINK #3 закрыта ручным стопом владельца (allOrders, positions,
# openOrders, GET ордера по orderId ручного условника).
LINK_MANUAL_STOP = Path(__file__).parent / "fixtures" / "bingx_demo_20260929_link_manual_stop.json"
# 29.09: публичные ручки без ключей — ticker, klines v3, premiumIndex, contracts;
# live и demo хосты, метки с суффиксом " (live)"/" (demo)".
PUBLIC = Path(__file__).parent / "fixtures" / "bingx_public_20260929.json"


@cache
def _calls(fixture: Path = FIXTURE) -> dict[str, dict[str, Any]]:
    data = json.loads(fixture.read_text(encoding="utf-8"))
    return {call["label"]: call for call in data["calls"]}


def _raw(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, field in item["fields"].items():
        value = field["value"]
        if isinstance(value, dict) and "fields" in value:
            value = _raw(value)
        out[key] = value
    return out


def live_items(label: str, fixture: Path = FIXTURE) -> list[dict[str, Any]]:
    """Сырые элементы ответа по метке вызова (см. "label" в фикстуре)."""
    return [_raw(item) for item in _calls(fixture)[label]["items"]]


def live_call(label: str, fixture: Path = FIXTURE) -> dict[str, Any]:
    """Метаданные вызова: code, msg, rate_remain, rate_expire_ms, keys."""
    return _calls(fixture)[label]
