"""Тесты настроек и хранения биржевых ключей.

Главная проверка: секрет не лежит в базе открытым текстом.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.config import Settings
from app.core.security import SecretCipher, mask_secret
from app.database.models.credentials import ExchangeCredentials
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)

D = Decimal
API_KEY = "bingx_public_key_abcdef123456"
API_SECRET = "bingx_private_secret_zyxwvu987654"


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        user = await svc.get_or_create(telegram_id=unique_telegram_id())
        cipher = SecretCipher(settings.encryption_key.get_secret_value())
        yield user, session, cipher
        await cleanup_user(session, user)
    await db.dispose()


async def test_secret_is_not_stored_in_plaintext(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, cipher = ctx

    session.add(
        ExchangeCredentials(
            user_id=user.id,
            exchange="bingx",
            api_key_encrypted=cipher.encrypt(API_KEY),
            api_secret_encrypted=cipher.encrypt(API_SECRET),
            api_key_masked=mask_secret(API_KEY),
        )
    )
    await session.flush()

    # Читаем сырую строку из базы, минуя ORM.
    row = (
        await session.execute(
            text(
                "SELECT api_key_encrypted, api_secret_encrypted, api_key_masked "
                "FROM exchange_credentials WHERE user_id = :uid"
            ),
            {"uid": user.id},
        )
    ).one()

    assert API_KEY not in row[0]
    assert API_SECRET not in row[1]
    assert API_SECRET not in row[2]
    # Маска показывает только края, чтобы отличать ключи между собой.
    assert row[2] == "bing...3456"


async def test_roundtrip_through_db(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, cipher = ctx

    session.add(
        ExchangeCredentials(
            user_id=user.id,
            exchange="bingx",
            api_key_encrypted=cipher.encrypt(API_KEY),
            api_secret_encrypted=cipher.encrypt(API_SECRET),
            api_key_masked=mask_secret(API_KEY),
        )
    )
    await session.flush()

    creds = await session.scalar(
        select(ExchangeCredentials).where(ExchangeCredentials.user_id == user.id)
    )
    assert creds is not None
    assert cipher.decrypt(creds.api_key_encrypted) == API_KEY
    assert cipher.decrypt(creds.api_secret_encrypted) == API_SECRET


async def test_one_credential_per_exchange(ctx) -> None:  # type: ignore[no-untyped-def]
    """UNIQUE(user_id, exchange) не даёт завести два ключа одной биржи."""
    from sqlalchemy.exc import IntegrityError

    user, session, cipher = ctx
    for _ in range(2):
        session.add(
            ExchangeCredentials(
                user_id=user.id,
                exchange="bingx",
                api_key_encrypted=cipher.encrypt(API_KEY),
                api_secret_encrypted=cipher.encrypt(API_SECRET),
                api_key_masked=mask_secret(API_KEY),
            )
        )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_live_and_demo_keys_coexist(ctx) -> None:  # type: ignore[no-untyped-def]
    """Этап 15.4в: UNIQUE(user_id, exchange, mode) — две пары ключей одного
    пользователя на одну биржу разрешены, если это разные режимы."""
    user, session, cipher = ctx
    for mode, key in ((ExchangeKeyMode.LIVE, API_KEY), (ExchangeKeyMode.DEMO, "demo_" + API_KEY)):
        session.add(
            ExchangeCredentials(
                user_id=user.id,
                exchange="bingx",
                mode=mode,
                api_key_encrypted=cipher.encrypt(key),
                api_secret_encrypted=cipher.encrypt(API_SECRET),
                api_key_masked=mask_secret(key),
            )
        )
    await session.flush()

    rows = list(
        await session.scalars(
            select(ExchangeCredentials).where(ExchangeCredentials.user_id == user.id)
        )
    )
    assert {row.mode for row in rows} == {ExchangeKeyMode.LIVE, ExchangeKeyMode.DEMO}


async def test_one_credential_per_exchange_and_mode(ctx) -> None:  # type: ignore[no-untyped-def]
    """UNIQUE(user_id, exchange, mode) не даёт завести два ключа одного
    режима — переключение счёта в настройках не должно требовать этого."""
    from sqlalchemy.exc import IntegrityError

    user, session, cipher = ctx
    for _ in range(2):
        session.add(
            ExchangeCredentials(
                user_id=user.id,
                exchange="bingx",
                mode=ExchangeKeyMode.DEMO,
                api_key_encrypted=cipher.encrypt(API_KEY),
                api_secret_encrypted=cipher.encrypt(API_SECRET),
                api_key_masked=mask_secret(API_KEY),
            )
        )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_active_exchange_mode_defaults_to_live(ctx) -> None:  # type: ignore[no-untyped-def]
    """Этап 15.4в, раздел "Модель данных": по умолчанию показывается LIVE."""
    user, session, _cipher = ctx
    settings_row = await UserRepository(session).get_settings(user.id)
    assert settings_row is not None
    assert settings_row.active_exchange_mode is ExchangeKeyMode.LIVE


async def test_switching_active_exchange_mode_keeps_both_key_pairs(ctx) -> None:  # type: ignore[no-untyped-def]
    """Переключение показа не трогает уже заведённые ключи ни одного режима."""
    user, session, cipher = ctx
    for mode, key in ((ExchangeKeyMode.LIVE, API_KEY), (ExchangeKeyMode.DEMO, "demo_" + API_KEY)):
        session.add(
            ExchangeCredentials(
                user_id=user.id,
                exchange="bingx",
                mode=mode,
                api_key_encrypted=cipher.encrypt(key),
                api_secret_encrypted=cipher.encrypt(API_SECRET),
                api_key_masked=mask_secret(key),
            )
        )
    await session.flush()

    settings_row = await UserRepository(session).get_settings(user.id)
    assert settings_row is not None
    settings_row.active_exchange_mode = ExchangeKeyMode.DEMO
    await session.flush()

    rows = list(
        await session.scalars(
            select(ExchangeCredentials).where(ExchangeCredentials.user_id == user.id)
        )
    )
    assert len(rows) == 2  # оба режима целы, переключение их не удаляет


async def test_plan_update_persists(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, _ = ctx
    repo = UserRepository(session)

    plan = await repo.get_trading_plan(user.id)
    assert plan is not None
    assert plan.risk_per_trade_percent == D("2")

    plan.risk_per_trade_percent = D("1.5")
    plan.max_trades_per_day = 3
    await session.flush()

    reloaded = await repo.get_trading_plan(user.id)
    assert reloaded is not None
    assert reloaded.risk_per_trade_percent == D("1.5")
    assert reloaded.max_trades_per_day == 3


async def test_plan_rejects_invalid_risk(ctx) -> None:  # type: ignore[no-untyped-def]
    """CHECK-ограничение в БД — последний рубеж, если валидация в коде обойдена."""
    from sqlalchemy.exc import IntegrityError

    user, session, _ = ctx
    plan = await UserRepository(session).get_trading_plan(user.id)
    assert plan is not None

    plan.risk_per_trade_percent = D("150")
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
