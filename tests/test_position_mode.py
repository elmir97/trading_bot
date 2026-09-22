"""Тесты app/services/position_mode.py — раздел 16 ТЗ, шаг 15.5.1.

В отличие от test_permissions.py TTL здесь не в колонке БД, а в
TTLCache — ни БД, ни DATABASE_URL не нужны, как и test_market_data.py.
"""

from __future__ import annotations

from app.exchanges.base import ExchangeAuthError, ExchangeError
from app.market.cache import TTLCache
from app.services.position_mode import refresh_position_mode


class FakeClient:
    name = "fake"

    def __init__(
        self, dual_side_position: bool | None = None, error: Exception | None = None
    ) -> None:
        self._dual_side_position = dual_side_position
        self._error = error
        self.calls = 0

    async def get_position_mode(self, *, max_retries: int | None = None) -> bool:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._dual_side_position is not None
        return self._dual_side_position


async def test_success_returns_trustworthy_outcome() -> None:
    cache = TTLCache()
    client = FakeClient(dual_side_position=True)

    outcome = await refresh_position_mode(cache, client, user_id=1, ttl_seconds=300)

    assert outcome.trustworthy is True
    assert outcome.dual_side_position is True
    assert outcome.error is None


async def test_second_call_within_ttl_uses_cache_not_network() -> None:
    cache = TTLCache()
    client = FakeClient(dual_side_position=True)

    await refresh_position_mode(cache, client, user_id=1, ttl_seconds=300)
    await refresh_position_mode(cache, client, user_id=1, ttl_seconds=300)

    assert client.calls == 1


async def test_different_user_id_does_not_share_cache_entry() -> None:
    """Ключ кэша включает user_id — раздел 16 ТЗ, шаг 15.5.1: приватные
    данные аккаунта, не общий market-data кэш."""
    cache = TTLCache()
    client = FakeClient(dual_side_position=True)

    await refresh_position_mode(cache, client, user_id=1, ttl_seconds=300)
    await refresh_position_mode(cache, client, user_id=2, ttl_seconds=300)

    assert client.calls == 2


async def test_failure_is_not_cached_next_call_retries() -> None:
    """Сбой не кладём в кэш — следующий вызов пробует заново, а не
    залипает на "неизвестно" до истечения TTL."""
    cache = TTLCache()
    client = FakeClient(error=ExchangeAuthError("биржа отклонила ключ"))

    first = await refresh_position_mode(cache, client, user_id=1, ttl_seconds=300)
    assert first.trustworthy is False
    assert first.dual_side_position is None
    assert isinstance(first.error, ExchangeError)
    assert client.calls == 1

    second = await refresh_position_mode(cache, client, user_id=1, ttl_seconds=300)
    assert second.trustworthy is False
    assert client.calls == 2  # не из кэша — реальная вторая попытка
