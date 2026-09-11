"""Тесты app/core/locks.py на настоящем FakeRedis (fakeredis[lua]).

tests/test_locks.py проверяет контракт RedisLock на упрощённом
Python-дублёре (FakeRedis из этого файла — не путать: там своя, ручная
реализация SET NX / EVAL). Здесь — тот же сценарий, но через
fakeredis.aioredis.FakeRedis, который реально исполняет Lua-скрипт
снятия (_RELEASE_SCRIPT) через lupa и реально считает TTL по времени.
Это ловит то, что ручной дублёр в принципе не может: опечатку в самом
Lua-скрипте или неверную арность EVAL.

lupa обязателен — без него FakeRedis.eval() падает
(fakeredis требует extra [lua], см. requirements-dev.txt).
"""

from __future__ import annotations

import asyncio

import pytest
from fakeredis.aioredis import FakeRedis

from app.core.locks import LockBusyError, RedisLock

KEY = "exec:lock:1:2"


@pytest.fixture
async def redis():
    r = FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def test_lock_acquires(redis: FakeRedis) -> None:
    async with RedisLock(redis, KEY, ttl_seconds=15):
        assert await redis.exists(KEY)
        ttl = await redis.ttl(KEY)
        assert 0 < ttl <= 15


async def test_second_acquire_while_held_raises_busy(redis: FakeRedis) -> None:
    async with RedisLock(redis, KEY, ttl_seconds=15):
        with pytest.raises(LockBusyError):
            async with RedisLock(redis, KEY, ttl_seconds=15):
                pass  # pragma: no cover — не должно сюда дойти


async def test_release_by_wrong_owner_does_not_remove_lock(redis: FakeRedis) -> None:
    """Настоящий Lua compare-and-delete: чужой токен не проходит проверку
    ``GET KEYS[1] == ARGV[1]`` в _RELEASE_SCRIPT, DEL не выполняется."""
    holder = RedisLock(redis, KEY, ttl_seconds=15)
    await holder.__aenter__()

    intruder = RedisLock(redis, KEY, ttl_seconds=15)  # свой токен, чужой ключ
    await intruder.__aexit__(None, None, None)

    assert await redis.exists(KEY)  # держатель не пострадал
    assert await redis.get(KEY) == holder._token

    await holder.__aexit__(None, None, None)  # уборка
    assert not await redis.exists(KEY)


async def test_ttl_expiry_frees_the_key(redis: FakeRedis) -> None:
    """Истечение TTL освобождает ключ и без явного release — упавший
    процесс (или зависший __aexit__) не блокирует ключ навсегда."""
    lock = RedisLock(redis, KEY, ttl_seconds=1)
    await lock.__aenter__()
    assert await redis.exists(KEY)

    await asyncio.sleep(1.2)
    assert not await redis.exists(KEY)  # снят по TTL, не по release

    # ключ свободен — новый держатель берёт без LockBusyError
    async with RedisLock(redis, KEY, ttl_seconds=15):
        assert await redis.exists(KEY)
