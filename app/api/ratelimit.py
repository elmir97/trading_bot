"""Ограничение частоты запросов Mini App на пользователя (§3): окно — секунда,
счётчик в Redis. preview 5/с, open/confirm/cancel 1/с — защита биржи и от
залипшей кнопки."""

from __future__ import annotations

import time
from typing import Any

from app.api.deps import ApiError

PREVIEW_PER_SECOND = 5
ACTION_PER_SECOND = 1
TOO_MANY_TEXT = "Слишком часто — подожди секунду."


def window() -> int:
    """Текущее окно — номер секунды (тесты подменяют)."""
    return int(time.time())


async def limit(redis: Any, user_id: int, bucket: str, per_second: int) -> None:
    key = f"ma:rl:{bucket}:{user_id}:{window()}"
    count = int(await redis.incr(key))
    if count == 1:
        await redis.expire(key, 2)
    if count > per_second:
        raise ApiError(429, "TOO_MANY_REQUESTS", TOO_MANY_TEXT)


__all__ = ["ACTION_PER_SECOND", "PREVIEW_PER_SECOND", "limit"]
