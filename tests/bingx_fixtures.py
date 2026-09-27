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


@cache
def _calls() -> dict[str, dict[str, Any]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {call["label"]: call for call in data["calls"]}


def _raw(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, field in item["fields"].items():
        value = field["value"]
        if isinstance(value, dict) and "fields" in value:
            value = _raw(value)
        out[key] = value
    return out


def live_items(label: str) -> list[dict[str, Any]]:
    """Сырые элементы ответа по метке вызова (см. "label" в фикстуре)."""
    return [_raw(item) for item in _calls()[label]["items"]]


def live_call(label: str) -> dict[str, Any]:
    """Метаданные вызова: code, msg, rate_remain, rate_expire_ms, keys."""
    return _calls()[label]
