"""Тесты app/services/permissions.py — раздел 8 ТЗ (проверка прав ключа).

Против настоящей БД (нужна реальная ExchangeCredentials — как
test_execution_service.py), биржа замокана классом-протоколом (только
get_api_restrictions, ничего больше не используется этим модулем).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.database.models.credentials import ExchangeCredentials
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ApiRestrictions, ExchangeAuthError, ExchangeError
from app.services.permissions import refresh_permissions
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

NOW = datetime.now(UTC)


class FakeClient:
    def __init__(
        self, restrictions: ApiRestrictions | None = None, error: ExchangeError | None = None
    ) -> None:
        self._restrictions = restrictions
        self._error = error

    async def get_api_restrictions(self) -> ApiRestrictions:
        if self._error is not None:
            raise self._error
        assert self._restrictions is not None
        return self._restrictions


def _restrictions(**overrides: object) -> ApiRestrictions:
    fields: dict[str, object] = {
        "ip_restrict": True, "create_time": NOW, "permits_universal_transfer": False,
        "enable_reading": True, "enable_futures": True, "enable_spot_and_margin_trading": False,
    }
    fields.update(overrides)
    return ApiRestrictions(**fields)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def ctx():  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        )
        telegram_id = 970_000 + int(datetime.now(UTC).timestamp() * 1000) % 20_000
        user = await user_service.get_or_create(telegram_id=telegram_id)
        creds = ExchangeCredentials(
            user_id=user.id, exchange="bingx", mode=ExchangeKeyMode.LIVE,
            api_key_encrypted="x", api_secret_encrypted="x", api_key_masked="x",
            is_read_only=True, is_active=True,
        )
        session.add(creds)
        await session.flush()
        yield session, creds
    await db.dispose()


async def test_enable_futures_false_sets_is_read_only_true(ctx) -> None:  # type: ignore[no-untyped-def]
    session, creds = ctx
    client = FakeClient(_restrictions(enable_futures=False))

    outcome = await refresh_permissions(session, creds, client, ttl_hours=6, now=NOW)

    assert outcome.attempted is True
    assert outcome.trustworthy is True
    assert outcome.error is None
    assert creds.is_read_only is True
    assert creds.permissions_checked_at == NOW


async def test_enable_futures_true_clears_is_read_only(ctx) -> None:  # type: ignore[no-untyped-def]
    session, creds = ctx
    client = FakeClient(_restrictions(enable_futures=True))

    await refresh_permissions(session, creds, client, ttl_hours=6, now=NOW)

    assert creds.is_read_only is False


async def test_fresh_mark_skips_network_call(ctx) -> None:  # type: ignore[no-untyped-def]
    """force=False (автопуть из карточки) и отметка ещё в пределах TTL —
    к бирже не идём вовсе, старое значение не трогаем."""
    session, creds = ctx
    creds.permissions_checked_at = NOW
    creds.is_read_only = False
    # Клиент без restrictions/error — если бы его всё-таки позвали, тест
    # упал бы на AssertionError внутри FakeClient.get_api_restrictions.
    client = FakeClient()

    outcome = await refresh_permissions(session, creds, client, ttl_hours=6, now=NOW)

    assert outcome.attempted is False
    assert outcome.trustworthy is True
    assert creds.is_read_only is False


async def test_stale_mark_triggers_call_and_updates(ctx) -> None:  # type: ignore[no-untyped-def]
    session, creds = ctx
    creds.permissions_checked_at = NOW - timedelta(hours=7)
    creds.is_read_only = False
    client = FakeClient(_restrictions(enable_futures=False))

    outcome = await refresh_permissions(session, creds, client, ttl_hours=6, now=NOW)

    assert outcome.attempted is True
    assert creds.is_read_only is True
    assert creds.permissions_checked_at == NOW


async def test_request_failure_with_fresh_mark_stays_trustworthy(ctx) -> None:  # type: ignore[no-untyped-def]
    """Кнопка "Проверить права" (force=True) дёрнула биржу, хотя отметка
    была ещё свежей, и запрос провалился — старое значение не хуже, чем
    секунду назад, PERMISSIONS_UNKNOWN здесь неуместен."""
    session, creds = ctx
    creds.permissions_checked_at = NOW
    creds.is_read_only = False
    client = FakeClient(error=ExchangeAuthError("биржа отклонила ключ"))

    outcome = await refresh_permissions(
        session, creds, client, ttl_hours=6, force=True, now=NOW
    )

    assert outcome.attempted is True
    assert outcome.trustworthy is True
    assert outcome.error is not None
    assert creds.is_read_only is False  # не тронуто
    assert creds.permissions_checked_at == NOW  # не тронуто


async def test_request_failure_with_stale_mark_is_not_trustworthy(ctx) -> None:  # type: ignore[no-untyped-def]
    session, creds = ctx
    creds.permissions_checked_at = NOW - timedelta(hours=7)
    client = FakeClient(error=ExchangeError("сеть моргнула"))

    outcome = await refresh_permissions(session, creds, client, ttl_hours=6, now=NOW)

    assert outcome.attempted is True
    assert outcome.trustworthy is False
    assert outcome.error is not None


async def test_request_failure_with_missing_mark_is_not_trustworthy(ctx) -> None:  # type: ignore[no-untyped-def]
    session, creds = ctx
    creds.permissions_checked_at = None
    client = FakeClient(error=ExchangeError("сеть моргнула"))

    outcome = await refresh_permissions(session, creds, client, ttl_hours=6, now=NOW)

    assert outcome.trustworthy is False
