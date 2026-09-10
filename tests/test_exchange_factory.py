"""Тесты app/services/exchange_factory.py (этап 15.4в).

Проверяем: клиент для LIVE-ключа идёт на боевой хост, для DEMO-ключа — на
VST-хост; отсутствие ключа нужного режима даёт понятный ExchangeAuthError
(не молчание и не подмена режима); has_credentials/get_credentials/
list_credentials различают режимы, а не первую попавшуюся пару ключей.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.core.security import SecretCipher, mask_secret
from app.database.models.credentials import ExchangeCredentials
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ExchangeAuthError
from app.services.exchange_factory import ExchangeFactory
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

API_KEY = "bingx_public_key_abcdef123456"
API_SECRET = "bingx_private_secret_zyxwvu987654"


@pytest_asyncio.fixture
async def ctx():  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        tg = 700_000 + int(datetime.now(UTC).timestamp() * 1_000_000) % 90_000
        user = await svc.get_or_create(telegram_id=tg)
        cipher = SecretCipher(settings.encryption_key.get_secret_value())
        factory = ExchangeFactory(settings, cipher)
        yield settings, user, session, cipher, factory
    await db.dispose()


async def _add_credentials(session, cipher, user_id: int, mode: ExchangeKeyMode) -> None:
    key = f"{mode.value.lower()}_{API_KEY}"
    session.add(
        ExchangeCredentials(
            user_id=user_id,
            exchange="bingx",
            mode=mode,
            api_key_encrypted=cipher.encrypt(key),
            api_secret_encrypted=cipher.encrypt(API_SECRET),
            api_key_masked=mask_secret(key),
        )
    )
    await session.flush()


async def test_live_key_uses_live_host(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, user, session, cipher, factory = ctx
    await _add_credentials(session, cipher, user.id, ExchangeKeyMode.LIVE)

    client = await factory.for_user(session, user.id, mode=ExchangeKeyMode.LIVE)
    try:
        assert client._base_url == settings.bingx_live_base_url  # type: ignore[attr-defined]
    finally:
        await client.close()


async def test_demo_key_uses_demo_vst_host(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, user, session, cipher, factory = ctx
    await _add_credentials(session, cipher, user.id, ExchangeKeyMode.DEMO)

    client = await factory.for_user(session, user.id, mode=ExchangeKeyMode.DEMO)
    try:
        assert client._base_url == settings.bingx_demo_base_url  # type: ignore[attr-defined]
        assert client._base_url != settings.bingx_live_base_url  # type: ignore[attr-defined]
    finally:
        await client.close()


async def test_missing_mode_key_raises_auth_error_without_falling_back(ctx) -> None:  # type: ignore[no-untyped-def]
    """Есть только LIVE-ключ — запрос DEMO-клиента не должен молча взять
    LIVE-ключ и постучаться не туда: явный отказ вместо подмены режима."""
    _settings, user, session, cipher, factory = ctx
    await _add_credentials(session, cipher, user.id, ExchangeKeyMode.LIVE)

    with pytest.raises(ExchangeAuthError):
        await factory.for_user(session, user.id, mode=ExchangeKeyMode.DEMO)


async def test_has_credentials_is_mode_specific(ctx) -> None:  # type: ignore[no-untyped-def]
    _settings, user, session, cipher, factory = ctx
    await _add_credentials(session, cipher, user.id, ExchangeKeyMode.LIVE)

    assert await factory.has_credentials(session, user.id, mode=ExchangeKeyMode.LIVE) is True
    assert await factory.has_credentials(session, user.id, mode=ExchangeKeyMode.DEMO) is False


async def test_list_credentials_returns_both_pairs(ctx) -> None:  # type: ignore[no-untyped-def]
    _settings, user, session, cipher, factory = ctx
    await _add_credentials(session, cipher, user.id, ExchangeKeyMode.LIVE)
    await _add_credentials(session, cipher, user.id, ExchangeKeyMode.DEMO)

    creds = await factory.list_credentials(session, user.id)
    assert set(creds) == {ExchangeKeyMode.LIVE, ExchangeKeyMode.DEMO}
    assert creds[ExchangeKeyMode.LIVE].mode is ExchangeKeyMode.LIVE
    assert creds[ExchangeKeyMode.DEMO].mode is ExchangeKeyMode.DEMO
