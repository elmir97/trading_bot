"""Поля лога неудачного чтения ордера по cid (деплой 4, 4.1).

Деплой 3 (фикс 1) завёл такой лог для входа открытия; 11.10 «Закрыть всё»
по #29 «не подтвердилось» без единой строки причины — тот же read-back
глотал ошибку молча. Один набор полей для всех мест, где читают ордер по
cid. Поля — по списку: ответ BingX целиком в лог не попадает (одна ручка
отдаёт apiKey эхом)."""

from __future__ import annotations

from typing import Any

from app.exchanges.base import ExchangeError, OrderFill, ReadbackIncomplete


def readback_failure(
    *, error: ExchangeError | None = None, fill: OrderFill | None = None
) -> dict[str, Any]:
    """error — чтение упало: тип, код, у ReadbackIncomplete недостающее поле
    и статус ответа. fill — ответ есть, но не тот (не FILLED): статус и
    исполненный объём."""
    extra: dict[str, Any] = {}
    if error is not None:
        extra["error"] = type(error).__name__
        extra["code"] = error.code
        if isinstance(error, ReadbackIncomplete):
            extra["field"] = error.field
            payload = error.payload or {}
            extra["status"] = str(payload.get("status", "")) or None
    elif fill is not None:
        extra["status"] = fill.status
        extra["executed_qty"] = str(fill.executed_qty)
    return extra


# Значения из ответа POST, которые можно печатать: остальные поля — только
# именами (форма ответа на POST маркета живьём не снята; ищем updateTime).
_POST_VALUES = ("status", "updateTime", "avgPrice", "executedQty", "commission")


def post_answer_fields(raw: dict[str, object]) -> dict[str, Any]:
    """Деплой 4, 4.2: какие поля пришли в ответе POST маркета — имена всех,
    значения только из _POST_VALUES."""
    extra: dict[str, Any] = {"post_fields": sorted(str(k) for k in raw)}
    for key in _POST_VALUES:
        if key in raw:
            extra[f"post_{key}"] = str(raw[key])
    return extra
