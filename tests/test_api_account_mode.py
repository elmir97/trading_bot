"""Счёт Mini App берётся из БД пользователя, не из разрешения исполнения."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy import delete, select

from app.api.deps import ApiError, selected_mode
from app.bot.handlers import settings as bot_settings
from app.database.models.user import UserSettings
from app.database.repositories.user import UserRepository
from app.exchanges.base import ExchangeUnavailableError
from app.trading.enums import ExchangeKeyMode
from tests.api_helpers import (  # noqa: F401
    FakeClient,
    FakeFactory,
    api_fixture,
    auth,
    client_for,
)
from tests.conftest import cleanup_user
from tests.test_api_open import BODY, _make_user


async def set_mode(api, mode):  # type: ignore[no-untyped-def]
    async with api.state().db.session() as session:
        row = await session.scalar(select(UserSettings).where(UserSettings.user_id == api.uid))
        row.active_exchange_mode = mode


class ModeFactory(FakeFactory):
    def __init__(self):  # type: ignore[no-untyped-def]
        super().__init__()
        self.reads = []
        self.fail_live = False

    async def for_user(self, session, user_id, *, mode):  # type: ignore[no-untyped-def]
        self.reads.append((user_id, mode))
        client = FakeClient(Decimal(101 if mode is ExchangeKeyMode.LIVE else 202))
        client.get_positions = AsyncMock(return_value=[])
        if self.fail_live and mode is ExchangeKeyMode.LIVE:
            client.fail = ExchangeUnavailableError("live down")
        return client


async def test_bot_toggle_saved_and_api_reads_same_account(api, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(bot_settings, "show_settings", AsyncMock())
    factory = ModeFactory()
    state = api.state(factory=factory)
    async with client_for(state) as c:
        for mode, equity in [(ExchangeKeyMode.DEMO, "202.00"),
                             (ExchangeKeyMode.LIVE, "101.00"),
                             (ExchangeKeyMode.DEMO, "202.00")]:
            if mode is not ExchangeKeyMode.DEMO or factory.reads:
                async with state.db.session() as session:
                    await bot_settings.toggle_exchange_mode(
                        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(id=api.uid), session,
                    )
            body = (await c.get("/api/me", headers=auth(api.tid))).json()
            assert (body["account_mode"], body["equity"]) == (mode.value, equity)
            assert body["flags"] == {"dry_run": True, "live_allowed": False}
            await c.get("/api/market/XRP-USDT", headers=auth(api.tid))
            positions = (await c.get("/api/positions", headers=auth(api.tid))).json()
            assert positions["account_mode"] == mode.value
            assert factory.reads[-1] == (api.uid, mode)
        assert state.settings.bingx_trading_mode == "demo"
        keys = state.market_cache._data
        assert f"api:positions:{api.uid}:LIVE" in keys
        assert f"api:positions:{api.uid}:DEMO" in keys
        assert f"api:leverage:{api.uid}:LIVE:XRP-USDT" in keys
        assert f"api:leverage:{api.uid}:DEMO:XRP-USDT" in keys


async def test_users_keep_separate_modes_and_balances(api, unique_telegram_id):  # type: ignore[no-untyped-def]
    tid = unique_telegram_id()
    state = api.state(factory=ModeFactory(), settings={
        "miniapp_allowed_telegram_ids": f"{api.tid},{tid}",
    })
    uid = await _make_user(state.db, state.settings, tid)
    try:
        async with state.db.session() as session:
            other = await UserRepository(session).get_by_telegram_id(tid)
            other.settings.active_exchange_mode = ExchangeKeyMode.LIVE
        async with client_for(state) as c:
            for who, mode, equity in [(tid, "LIVE", "101.00"),
                                      (api.tid, "DEMO", "202.00"), (tid, "LIVE", "101.00")]:
                body = (await c.get("/api/me", headers=auth(who))).json()
                assert (body["account_mode"], body["equity"]) == (mode, equity)
                await c.get("/api/market/XRP-USDT", headers=auth(who))
        assert (api.uid, ExchangeKeyMode.DEMO) in state.factory.reads
        assert (uid, ExchangeKeyMode.LIVE) in state.factory.reads
    finally:
        async with state.db.session() as session:
            other = await UserRepository(session).get_by_telegram_id(tid)
            await cleanup_user(session, other)


@pytest.mark.parametrize(("selected", "allowed"), [
    (ExchangeKeyMode.LIVE, "demo"), (ExchangeKeyMode.DEMO, "live"),
])
async def test_selected_account_does_not_change_execution_mode(api, selected, allowed):  # type: ignore[no-untyped-def]
    await set_mode(api, selected)
    factory = ModeFactory()
    state = api.state(factory=factory, settings={
        "bingx_trading_mode": allowed, "exec_allow_live_mode_orders": True,
        "exec_open_allow_live": True,
    })
    state.redis = FakeRedis()
    try:
        async with client_for(state) as c:
            result = await c.post("/api/open/preview", json=BODY, headers=auth(api.tid))
    finally:
        await state.redis.aclose()
    assert result.status_code == 200
    assert result.json()["refusal"]["code"] == "MODE_NOT_ALLOWED"
    assert factory.reads == []


async def test_live_balance_error_never_returns_demo(api):  # type: ignore[no-untyped-def]
    factory = ModeFactory()
    state = api.state(factory=factory)
    async with client_for(state) as c:
        assert (await c.get("/api/me", headers=auth(api.tid))).json()["equity"] == "202.00"
        await set_mode(api, ExchangeKeyMode.LIVE)
        factory.fail_live = True
        body = (await c.get("/api/me", headers=auth(api.tid))).json()
        assert body["account_mode"] == "LIVE"
        assert body["equity"] is None and body["available"] is None
        assert body["balance_error"]
        assert factory.reads[-1] == (api.uid, ExchangeKeyMode.LIVE)


async def test_missing_settings_refuses_before_exchange(api):  # type: ignore[no-untyped-def]
    factory = ModeFactory()
    async with api.state().db.session() as session:
        await session.execute(delete(UserSettings).where(UserSettings.user_id == api.uid))
    async with client_for(api.state(factory=factory)) as c:
        for path in ["/api/me", "/api/positions", "/api/market/XRP-USDT"]:
            r = await c.get(path, headers=auth(api.tid))
            assert r.status_code == 409 and r.json()["code"] == "ACCOUNT_MODE_UNKNOWN"
    assert factory.reads == []


@pytest.mark.parametrize("mode", [None, "LIVE", "unknown"])
def test_unknown_mode_does_not_default_to_live(mode):  # type: ignore[no-untyped-def]
    with pytest.raises(ApiError, match="ACCOUNT_MODE_UNKNOWN"):
        selected_mode(SimpleNamespace(settings=SimpleNamespace(active_exchange_mode=mode)))
