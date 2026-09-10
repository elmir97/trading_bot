"""Кэш рыночных данных.

Смысл кэша не в экономии трафика, а в соблюдении лимитов биржи и в
скорости ответа бота. Но кэш опасен: показать вчерашнюю цену как
текущую хуже, чем не показать ничего.

Поэтому TTL задаётся отдельно для каждого типа данных и привязан к тому,
как быстро они устаревают по существу:

  цена        — 5 секунд, дальше она уже неверна;
  свечи       — до закрытия текущей свечи, ведь закрытые не меняются;
  инструменты — час, список контрактов меняется редко;
  баланс      — 30 секунд, компромисс между свежестью и лимитами.

Закрытые свечи — единственные по-настоящему неизменяемые данные, и
именно на них строится весь технический анализ.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# TTL в секундах.
TTL_TICKER = 5
TTL_KLINES = 30
TTL_SYMBOLS = 3600
TTL_BALANCE = 30
TTL_POSITIONS = 10
TTL_FUNDING = 300


@dataclass(slots=True)
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TTLCache:
    """Кэш в памяти процесса с раздельной блокировкой на ключ.

    Блокировка нужна, чтобы при одновременном промахе по одному ключу
    к бирже ушёл ровно один запрос, а не столько, сколько было
    обращений. На лимитах API это заметно.
    """

    def __init__(self) -> None:
        self._data: dict[str, _Entry[Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.hits = 0
        self.misses = 0

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if time.monotonic() >= entry.expires_at:
            del self._data[key]
            return None
        return entry.value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = _Entry(value=value, expires_at=time.monotonic() + ttl)

    def invalidate(self, prefix: str = "") -> int:
        """Сброс кэша. Без префикса — полностью."""
        keys = [k for k in self._data if k.startswith(prefix)]
        for key in keys:
            del self._data[key]
        return len(keys)

    async def get_or_fetch(self, key: str, ttl: float, factory):  # type: ignore[no-untyped-def]
        """Возвращает значение из кэша либо получает его через factory."""
        cached = self.get(key)
        if cached is not None:
            self.hits += 1
            return cached

        async with self._lock_for(key):
            # Повторная проверка: пока ждали блокировку, значение мог
            # положить другой вызов.
            cached = self.get(key)
            if cached is not None:
                self.hits += 1
                return cached

            self.misses += 1
            value = await factory()
            self.set(key, value, ttl)
            return value

    @property
    def size(self) -> int:
        return len(self._data)

    def stats(self) -> dict[str, int]:
        return {"size": self.size, "hits": self.hits, "misses": self.misses}
