"""Проверка Redis в проде: доступность + реальный цикл RedisLock.

Замена одноразовым скриптам, которые писались и удалялись вручную при
разворачивании этапа 15.4а (Redis для блокировки от двойного нажатия
"Да", раздел 8 ТЗ). Использует настоящий redis.asyncio.Redis по
get_settings().redis_url — тем же путём, что и app/main.py — и ключ,
который не пересекается с боевыми (exec:lock:__check__:0 не может
возникнуть от confirm_lock_key: user_id из Telegram не бывает "__check__").

Ничего не оставляет после себя: снимает свой ключ в finally, даже если
какая-то проверка упадёт на середине.

Запуск на проде:
    docker compose exec bot python -m scripts.check_redis
"""

from __future__ import annotations

import asyncio
import sys

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.locks import LockBusyError, RedisLock

CHECK_KEY = "exec:lock:__check__:0"
TTL_SECONDS = 5

problems: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✓ {name}")
    else:
        problems.append(f"{name}: {detail}")
        print(f"  ✗ {name} — {detail}")


async def check_lock_cycle(redis: Redis) -> None:
    await redis.delete(CHECK_KEY)  # чистый старт, если прошлый прогон упал

    print("\n[Lock] Захват")
    lock1 = RedisLock(redis, CHECK_KEY, ttl_seconds=TTL_SECONDS)
    await lock1.__aenter__()
    ttl = await redis.ttl(CHECK_KEY)
    check("ключ выставлен с TTL", 0 < ttl <= TTL_SECONDS, f"ttl={ttl}")

    print("\n[Lock] Отказ занятого")
    lock2 = RedisLock(redis, CHECK_KEY, ttl_seconds=TTL_SECONDS)
    try:
        await lock2.__aenter__()
        check("повторный захват отклонён", False, "неожиданно получилось")
        await lock2.__aexit__(None, None, None)
    except LockBusyError:
        check("повторный захват отклонён", True)

    print("\n[Lock] Снятие чужим владельцем")
    intruder = RedisLock(redis, CHECK_KEY, ttl_seconds=TTL_SECONDS)  # свой токен
    await intruder.__aexit__(None, None, None)
    still_there = await redis.exists(CHECK_KEY)
    check("чужой release не снял ключ", bool(still_there), "ключ пропал")

    print("\n[Lock] Снятие владельцем")
    await lock1.__aexit__(None, None, None)
    gone = await redis.exists(CHECK_KEY)
    check("владелец снял ключ", not gone, "ключ остался")

    print("\n[Lock] Повторный захват после снятия")
    lock3 = RedisLock(redis, CHECK_KEY, ttl_seconds=TTL_SECONDS)
    await lock3.__aenter__()
    check("захват после release", await redis.exists(CHECK_KEY))
    await lock3.__aexit__(None, None, None)  # уборка


async def main() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        print("[Ping]")
        pong = await redis.ping()
        check(f"PING {settings.redis_url}", pong is True, f"ответ={pong!r}")

        await check_lock_cycle(redis)
    finally:
        # Уборка на случай, если какая-то проверка упала до штатного release.
        await redis.delete(CHECK_KEY)
        await redis.aclose()

    print("\n" + "=" * 60)
    if problems:
        print(f"ПРОБЛЕМЫ ({len(problems)}):")
        for p in problems:
            print(f"  • {p}")
        sys.exit(1)
    print("Redis и RedisLock в порядке.")


if __name__ == "__main__":
    asyncio.run(main())
