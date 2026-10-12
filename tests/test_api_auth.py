"""API Mini App, подэтап 1.3: initData (§4), /api/health, /api/me, процесс API.

Подпись проверяется и независимой реализацией (aiogram.utils.web_app) — та же
спецификация Telegram, другой код."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode

import pytest
import pytest_asyncio
from aiogram.utils.web_app import check_webapp_signature

from app.api.auth import EXPIRED_TEXT, InitDataError, validate_init_data
from app.api.deps import ApiState
from app.core.config import Settings
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ExchangeUnavailableError
from app.services.user_service import UserService
from tests.api_helpers import TOKEN, FakeClient, FakeFactory, auth, client_for, init_data
from tests.conftest import cleanup_user

D = Decimal
TTL = 3600
NOW = int(time.time())


def _validate(raw: str, **kwargs):  # type: ignore[no-untyped-def]
    return validate_init_data(raw, TOKEN, ttl_seconds=TTL, **kwargs)


def _tamper(raw: str, key: str, value: str) -> str:
    pairs = [(k, value if k == key else v) for k, v in parse_qsl(raw)]
    return urlencode(pairs)


# --- подпись и срок -----------------------------------------------------------------


def test_valid_init_data() -> None:
    raw = init_data(777000111)
    assert check_webapp_signature(TOKEN, raw)          # независимая реализация согласна
    data = _validate(raw)
    assert data.telegram_id == 777000111
    assert data.auth_date.tzinfo is UTC


def test_signature_field_is_signed_like_any_other() -> None:
    """signature (Bot API 8.0) участвует в data_check_string как обычное поле."""
    raw = init_data(1, extra={"signature": "c2lnbmF0dXJl"})
    assert check_webapp_signature(TOKEN, raw) and _validate(raw).telegram_id == 1
    unsigned = urlencode([*parse_qsl(init_data(1)), ("signature", "added")])
    with pytest.raises(InitDataError) as err:
        _validate(unsigned)
    assert err.value.reason == "bad_hash"


@pytest.mark.parametrize(("raw", "reason"), [
    (_tamper(init_data(5), "user", '{"id":6}'), "bad_hash"),        # подменён пользователь
    (init_data(5, token="0:other"), "bad_hash"),                     # чужой токен
    ("&".join(p for p in init_data(5).split("&") if not p.startswith("hash=")), "no_hash"),
    (init_data(5) + "&auth_date=1", "duplicate_key"),
    ("not a query", "unparsable"),
    (init_data(5, user={"first_name": "no id"}), "no_user"),
    (init_data(5, user={"id": "5"}), "bad_user_id"),
    (init_data(5, user={"id": True}), "bad_user_id"),
    (init_data(5, user=[5]), "no_user"),
])
def test_rejected(raw: str, reason: str) -> None:
    with pytest.raises(InitDataError) as err:
        _validate(raw)
    assert err.value.reason == reason


def test_expiry_and_future() -> None:
    now = datetime.fromtimestamp(NOW, tz=UTC)
    assert _validate(init_data(5, auth_date=NOW - TTL), now=now).telegram_id == 5
    assert _validate(init_data(5, auth_date=NOW + 60), now=now).telegram_id == 5
    for auth_date, reason in ((NOW - TTL - 1, "expired"), (NOW + 61, "auth_date_future")):
        with pytest.raises(InitDataError) as err:
            _validate(init_data(5, auth_date=auth_date), now=now)
        assert (err.value.reason, err.value.message) == (reason, EXPIRED_TEXT)


def test_settings_allowed_ids() -> None:
    base = {"bot_token": TOKEN, "database_url": "postgresql+asyncpg://u:p@h/d",
            "encryption_key": "0" * 43 + "="}
    assert Settings(**base).miniapp_allowed_ids == frozenset()  # type: ignore[arg-type]
    assert Settings(miniapp_allowed_telegram_ids="12, 34", **base).miniapp_allowed_ids == {12, 34}  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="MINIAPP_ALLOWED_TELEGRAM_IDS"):
        Settings(miniapp_allowed_telegram_ids="12,abc", **base)  # type: ignore[arg-type]


# --- эндпоинты на настоящей БД ---------------------------------------------------------

needs_db = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


@pytest_asyncio.fixture
async def api(unique_telegram_id):  # type: ignore[no-untyped-def]
    tid = unique_telegram_id()
    settings = Settings(  # type: ignore[call-arg]
        bot_token=TOKEN, trading_execution_enabled=True, bingx_trading_mode="demo",
        miniapp_allowed_telegram_ids=str(tid),
    )
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=tid)
        await session.commit()
        uid = user.id

    def state(**kwargs):  # type: ignore[no-untyped-def]
        s = settings.model_copy(update=kwargs.pop("settings", {}))
        return ApiState(settings=s, db=db, redis=None, factory=kwargs.pop("factory", FakeFactory()))

    yield SimpleNamespace(tid=tid, uid=uid, state=state, settings=settings)
    async with db.session() as session:
        fresh = await UserRepository(session).get_by_id(uid)
        if fresh is not None:
            await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


@needs_db
async def test_health_without_auth(api) -> None:  # type: ignore[no-untyped-def]
    async with client_for(api.state()) as c:
        r = await c.get("/api/health")
    assert (r.status_code, r.json()) == (200, {"ok": True})


@needs_db
async def test_me_requires_auth(api) -> None:  # type: ignore[no-untyped-def]
    async with client_for(api.state()) as c:
        none = await c.get("/api/me")
        bearer = {"Authorization": f"Bearer {init_data(api.tid)}"}
        wrong_scheme = await c.get("/api/me", headers=bearer)
        expired = await c.get("/api/me", headers=auth(api.tid, auth_date=NOW - TTL - 5))
        # те же 4 символа, что «tma », но другая схема — initData дальше валиден
        same_len = await c.get("/api/me", headers={"Authorization": f"xyz {init_data(api.tid)}"})
    assert [r.status_code for r in (none, wrong_scheme, expired, same_len)] == [401] * 4
    assert none.json()["code"] == "UNAUTHORIZED"
    assert expired.json() == {"code": "UNAUTHORIZED", "message": EXPIRED_TEXT}


@needs_db
async def test_me_forbidden(api, unique_telegram_id) -> None:  # type: ignore[no-untyped-def]
    stranger = unique_telegram_id()                       # подпись верна, не в списке
    nobody = unique_telegram_id()                         # в списке, но нет в users
    async with client_for(api.state()) as c:
        r1 = await c.get("/api/me", headers=auth(stranger))
    closed = api.state(settings={"miniapp_allowed_telegram_ids": ""})   # пусто — 403 всем
    async with client_for(closed) as c:
        r2 = await c.get("/api/me", headers=auth(api.tid))
    listed = api.state(settings={"miniapp_allowed_telegram_ids": f"{api.tid},{nobody}"})
    async with client_for(listed) as c:
        r3 = await c.get("/api/me", headers=auth(nobody))
    assert [r.status_code for r in (r1, r2, r3)] == [403, 403, 403]
    assert {r.json()["code"] for r in (r1, r2, r3)} == {"FORBIDDEN"}


@needs_db
async def test_me(api, caplog) -> None:  # type: ignore[no-untyped-def]
    caplog.set_level(logging.DEBUG)
    client = FakeClient()
    async with client_for(api.state(factory=FakeFactory(client))) as c:
        headers = auth(api.tid)
        r = await c.get("/api/me", headers=headers)
        rejected = await c.get("/api/me", headers=auth(api.tid, token="0:other"))
    assert r.status_code == 200 and rejected.status_code == 401
    body = r.json()
    assert body["account_mode"] == "DEMO"
    assert (body["equity"], body["available"]) == ("1500.46", "1475.46")
    assert body["balance_error"] is None
    plan = body["plan"]
    assert plan["max_leverage"] is not None and isinstance(plan["allowed_symbols"], list)
    assert (plan["risk_percent"], plan["min_rr"]) == ("2.00", "2.00")    # строкой, 2 знака
    assert body["flags"] == {"dry_run": True, "live_allowed": False}
    assert body["limit_expiry_minutes"] == [15, 60, 240, 1440]
    assert (body["limit_expiry_default"], body["card_ttl_seconds"]) == (240, 120)
    assert client.closed
    # initData и hash — никогда в логе (ни в тексте, ни в extra)
    secret = headers["Authorization"].removeprefix("tma ")
    hash_value = dict(parse_qsl(secret))["hash"]
    for record in caplog.records:
        dump = f"{record.getMessage()} {record.__dict__}"
        assert secret not in dump and hash_value not in dump
    assert any(getattr(r, "reason", None) == "bad_hash" for r in caplog.records)


@needs_db
async def test_me_balance_unavailable(api) -> None:  # type: ignore[no-untyped-def]
    no_key = api.state(factory=FakeFactory(has_key=False))
    broken = FakeClient(fail=ExchangeUnavailableError("down"))
    async with client_for(no_key) as c:
        r1 = (await c.get("/api/me", headers=auth(api.tid))).json()
    async with client_for(api.state(factory=FakeFactory(broken))) as c:
        r2 = (await c.get("/api/me", headers=auth(api.tid))).json()
    assert r1["equity"] is None and "Ключ BingX не подключён" in r1["balance_error"]
    assert r2["equity"] is None and "ExchangeUnavailableError" in r2["balance_error"]
    assert broken.closed


# --- процесс API ------------------------------------------------------------------------


def test_api_process_is_light() -> None:
    """Импорт и create_app() без окружения: не читает настройки, не тянет
    графики (matplotlib/pandas), планировщик и хендлеры бота (§7)."""
    code = (
        "import sys\n"
        "from app.api.main import create_app\n"
        "create_app()\n"
        "heavy = ('matplotlib', 'pandas', 'mplfinance', 'apscheduler', 'app.bot.handlers',"
        " 'app.workers.scheduler')\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in heavy or m.startswith(heavy))\n"
        "print(bad); sys.exit(1 if bad else 0)\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in {"DATABASE_URL", "BOT_TOKEN", "ENCRYPTION_KEY"}}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=env, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
