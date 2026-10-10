"""Блокировка от двойного нажатия (этап 15, раздел 8 ТЗ).

Redis, а не блокировка в памяти процесса: бот когда-нибудь может работать
несколькими воркерами (см. docstring build_dispatcher про FSM-хранилище),
и защита от гонки обязана переживать это без переписывания. Токен + Lua-
скрипт на снятие — стандартный паттерн: освободить ключ должен только тот,
кто его поставил, иначе после TTL мог бы снять чужую, ещё активную
блокировку (RELEASE двух держателей подряд).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from types import TracebackType
from typing import Protocol

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class RedisLike(Protocol):
    """Минимум, который нужен RedisLock — именно этот срез покрывают и
    redis.asyncio.Redis, и тестовый дублёр (см. tests/test_locks.py)."""

    async def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> object: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object: ...


class LockBusyError(RuntimeError):
    """Ключ уже занят другим держателем.

    Вызывающий код обязан ответить пользователю «уже обрабатывается»
    (раздел 8 ТЗ), а не повторить попытку и не проигнорировать нажатие.
    """


class RedisLock:
    """Блокировка на один callback: ``async with RedisLock(...):``.

    Бросает LockBusyError, если ключ уже занят — по умолчанию сама не ждёт и
    не ретраит: повторное нажатие должно получить мгновенный ответ, а не
    зависнуть в очереди на тот же ордер. wait_seconds > 0 — только для
    «🔴 Да, закрыть» под тревогой (деплой 3, фикс 5): лок держит фоновый
    повтор защиты на 3–5 с, кнопка дожидается его, опрашивая ключ.
    """

    def __init__(
        self, redis: RedisLike, key: str, ttl_seconds: int, *,
        wait_seconds: float = 0.0, poll_seconds: float = 0.25,
    ) -> None:
        self._redis = redis
        self._key = key
        self._ttl = ttl_seconds
        self._token = secrets.token_hex(16)
        self._wait = wait_seconds
        self._poll = poll_seconds

    async def __aenter__(self) -> RedisLock:
        deadline = time.monotonic() + self._wait
        while not await self._redis.set(self._key, self._token, nx=True, ex=self._ttl):
            if time.monotonic() >= deadline:
                raise LockBusyError(f"Ключ {self._key} уже занят")
            await asyncio.sleep(self._poll)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._redis.eval(_RELEASE_SCRIPT, 1, self._key, self._token)


def confirm_lock_key(user_id: int, notification_id: int) -> str:
    """Раздел 8 ТЗ: exec:lock:{user_id}:n{notification_id}. Буква "n" — шаг
    15.5.2а: ключ адресует снимок уведомления, а не слот (прежний формат
    exec:lock:{user_id}:{signal_id}), и два формата не путаются."""
    return f"exec:lock:{user_id}:n{notification_id}"


def position_lock_key(user_id: int, symbol: str, side: str) -> str:
    """Этап 4: лок действия с позицией — exec:lock:{user_id}:p{symbol}:{side}.
    Одна позиция — одно действие в полёте; префикс exec:lock: тот же, что у
    входа, — reconciler пропускает цикл, пока жив любой такой ключ."""
    return f"exec:lock:{user_id}:p{symbol}:{side}"


def close_wanted_key(opening_id: int) -> str:
    """Деплой 3, фикс 5: владелец нажал «🔴 Да, закрыть» и ждёт лок — фоновые
    повторы защиты и цикл уступают. Не exec:lock:* — reconciler его не ждёт."""
    return f"exec:close_wanted:o{opening_id}"
