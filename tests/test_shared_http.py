"""Общий httpx-клиент биржи на процесс api (SharedHttp, 17.10).

Проверяем: один httpx.AsyncClient на хост, клиенты биржи api (публичный и с
ключами, LIVE и DEMO) идут через него, close() клиента биржи его не закрывает,
закрывает lifespan api; cookies не принимаются; без SharedHttp (бот) — как
раньше: у каждого клиента свой httpx-клиент, close() его закрывает.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.api import main as api_main
from app.core.config import Settings
from app.core.security import SecretCipher, mask_secret
from app.database.models.credentials import ExchangeCredentials
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.exchange_factory import ExchangeFactory, SharedHttp
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode
from tests.conftest import cleanup_user

needs_db = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


async def test_one_client_per_host_and_aclose_closes_all() -> None:
    http = SharedHttp(timeout=3.0)
    a = http.client("https://open-api.bingx.com")
    assert http.client("https://open-api.bingx.com/") is a
    b = http.client("https://open-api-vst.bingx.com")
    assert b is not a
    assert a.timeout.read == 3.0
    await http.aclose()
    assert a.is_closed and b.is_closed
    # после aclose — новый клиент, не закрытый
    c = http.client("https://open-api.bingx.com")
    assert c is not a and not c.is_closed
    await http.aclose()


async def test_public_client_uses_shared_and_close_keeps_it_open() -> None:
    settings = _settings()
    http = SharedHttp(timeout=settings.http_timeout_seconds)
    factory = ExchangeFactory(settings, None, http=http)  # type: ignore[arg-type]
    first, second = factory.public_client(), factory.public_client()
    shared = http.client(settings.bingx_base_url)
    assert first._client is shared and second._client is shared  # type: ignore[attr-defined]
    await first.close()
    assert not shared.is_closed
    await factory.aclose()
    assert shared.is_closed


async def test_without_shared_each_client_owns_and_closes() -> None:
    """Бот: ExchangeFactory без http — поведение до 17.10."""
    factory = ExchangeFactory(_settings(), None)  # type: ignore[arg-type]
    first, second = factory.public_client(), factory.public_client()
    own = first._client  # type: ignore[attr-defined]
    assert own is not second._client  # type: ignore[attr-defined]
    await first.close()
    await second.close()
    assert own.is_closed
    await factory.aclose()          # без http — ничего не делает


async def test_shared_client_does_not_keep_cookies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {}},
                              headers={"Set-Cookie": "__cf_bm=abc; Path=/; Domain=.bingx.com"})

    http = SharedHttp(timeout=3.0, transport=httpx.MockTransport(handler))
    client = http.client("https://open-api.bingx.com")
    await client.get("/openApi/swap/v2/quote/price")
    sent = await client.get("/openApi/swap/v2/quote/price")
    assert len(client.cookies.jar) == 0
    assert "cookie" not in sent.request.headers
    await http.aclose()


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = _settings()
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(UserRepository(session), StrategyRepository(session),
                          MistakeTypeRepository(session), settings)
        user = await svc.get_or_create(telegram_id=unique_telegram_id())
        cipher = SecretCipher(settings.encryption_key.get_secret_value())
        for mode in (ExchangeKeyMode.LIVE, ExchangeKeyMode.DEMO):
            key = f"{mode.value.lower()}_shared_http_key_123456"
            session.add(ExchangeCredentials(
                user_id=user.id, exchange="bingx", mode=mode,
                api_key_encrypted=cipher.encrypt(key),
                api_secret_encrypted=cipher.encrypt("shared_http_secret_654321"),
                api_key_masked=mask_secret(key),
            ))
        await session.flush()
        yield settings, user, session, cipher
        await cleanup_user(session, user)
    await db.dispose()


@needs_db
async def test_for_user_uses_shared_client_of_its_host(ctx) -> None:  # type: ignore[no-untyped-def]
    settings, user, session, cipher = ctx
    http = SharedHttp(timeout=settings.http_timeout_seconds)
    factory = ExchangeFactory(settings, cipher, http=http)
    live = await factory.for_user(session, user.id, mode=ExchangeKeyMode.LIVE)
    demo = await factory.for_user(session, user.id, mode=ExchangeKeyMode.DEMO)
    assert live._client is http.client(settings.bingx_live_base_url)  # type: ignore[attr-defined]
    assert demo._client is http.client(settings.bingx_demo_base_url)  # type: ignore[attr-defined]
    assert live._client is not demo._client  # type: ignore[attr-defined]
    # ключ — у клиента биржи, не у общего httpx-клиента
    assert "X-BX-APIKEY" not in live._client.headers  # type: ignore[attr-defined]
    await live.close()
    await demo.close()
    assert not live._client.is_closed  # type: ignore[attr-defined]
    await factory.aclose()
    assert live._client.is_closed and demo._client.is_closed  # type: ignore[attr-defined]


async def test_build_state_gives_api_a_shared_http() -> None:
    state, worker = api_main.build_state(_settings())
    try:
        assert isinstance(state.factory._http, SharedHttp)
        public = state.factory.public_client()
        assert public._client is state.factory._http.client(state.settings.bingx_base_url)  # type: ignore[attr-defined]
        await public.close()
    finally:
        await worker.close()
        await state.bot.session.close()
        await state.factory.aclose()
        await state.redis.aclose()
        await state.db.dispose()


async def test_lifespan_closes_shared_http(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = _settings()
    http = SharedHttp(timeout=settings.http_timeout_seconds)
    factory = ExchangeFactory(settings, None, http=http)  # type: ignore[arg-type]
    shared = http.client(settings.bingx_base_url)

    async def noop() -> None:
        return None

    async def get_me() -> SimpleNamespace:
        return SimpleNamespace(username="postfactumTrade_bot")

    built = SimpleNamespace(
        bot=SimpleNamespace(get_me=get_me, session=SimpleNamespace(close=noop)),
        confirm_tasks={}, bot_username=None, factory=factory,
        redis=SimpleNamespace(aclose=noop), db=SimpleNamespace(dispose=noop),
    )
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(api_main, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(api_main, "build_state",
                        lambda s: (built, SimpleNamespace(close=noop)))
    app = api_main.create_app()
    async with app.router.lifespan_context(app):
        assert not shared.is_closed
    assert shared.is_closed
