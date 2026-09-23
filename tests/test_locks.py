"""Тесты app/core/locks.py (этап 15.4, раздел 8 ТЗ).

FakeRedis — не настоящий redis.asyncio.Redis: воспроизводит только тот
срез поведения (SET NX EX, EVAL compare-and-delete), которым пользуется
RedisLock, без сети и без живого Redis-сервера (RedisLike — протокол
именно под этот срез, см. app/core/locks.py).
"""

from __future__ import annotations

import pytest

from app.core.locks import LockBusyError, RedisLock, confirm_lock_key


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        key, token = keys_and_args[0], keys_and_args[1]
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


async def test_lock_acquires_and_releases() -> None:
    redis = FakeRedis()
    async with RedisLock(redis, "exec:lock:1:2", ttl_seconds=15):
        assert "exec:lock:1:2" in redis.store
    assert "exec:lock:1:2" not in redis.store  # снято при выходе из контекста


async def test_second_acquire_while_held_raises_busy() -> None:
    """Раздел 8 ТЗ: повторное нажатие "Да" получает мгновенный отказ, а не
    ждёт своей очереди на тот же ордер."""
    redis = FakeRedis()
    async with RedisLock(redis, "exec:lock:1:2", ttl_seconds=15):
        with pytest.raises(LockBusyError):
            async with RedisLock(redis, "exec:lock:1:2", ttl_seconds=15):
                pass  # pragma: no cover — не должно сюда дойти


async def test_lock_can_be_reacquired_after_release() -> None:
    redis = FakeRedis()
    async with RedisLock(redis, "exec:lock:1:2", ttl_seconds=15):
        pass
    async with RedisLock(redis, "exec:lock:1:2", ttl_seconds=15):
        assert "exec:lock:1:2" in redis.store


async def test_different_keys_do_not_conflict() -> None:
    redis = FakeRedis()
    async with (
        RedisLock(redis, "exec:lock:1:2", ttl_seconds=15),
        RedisLock(redis, "exec:lock:1:3", ttl_seconds=15),
    ):
        assert set(redis.store) == {"exec:lock:1:2", "exec:lock:1:3"}


async def test_release_does_not_remove_a_lock_it_does_not_own() -> None:
    """Токен + Lua-скрипт: снять чужую блокировку (например, поставленную
    заново после истечения TTL) нельзя, только свою."""
    redis = FakeRedis()
    lock = RedisLock(redis, "exec:lock:1:2", ttl_seconds=15)
    await lock.__aenter__()

    # Кто-то другой (или тот же ключ после TTL) успел перезахватить ключ.
    redis.store["exec:lock:1:2"] = "someone-elses-token"

    await lock.__aexit__(None, None, None)
    assert redis.store["exec:lock:1:2"] == "someone-elses-token"  # не снята


async def test_lock_released_even_if_body_raises() -> None:
    redis = FakeRedis()
    with pytest.raises(ValueError):
        async with RedisLock(redis, "exec:lock:1:2", ttl_seconds=15):
            raise ValueError("boom")
    assert "exec:lock:1:2" not in redis.store


def test_confirm_lock_key_format() -> None:
    """Шаг 15.5.2а: ключ адресует уведомление, буква "n" отличает его от
    прежнего формата exec:lock:{user_id}:{signal_id}."""
    assert confirm_lock_key(user_id=42, notification_id=7) == "exec:lock:42:n7"
