"""Локальный стенд фронта `scripts/miniapp_dev.py` (docs/miniapp-plan.md §9а):
страховки от прода, только 127.0.0.1, заголовки как у Caddy, обход авторизации
— только в стенде."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from app.api.deps import ApiState, current_user
from app.core.config import Settings
from app.database.repositories.user import UserRepository
from app.database.session import Database
from scripts import miniapp_dev as stand

_ROOT = Path(__file__).resolve().parents[1]
_TEST_DB = "postgresql+asyncpg://test:test@localhost:5432/trading_bot_test"
_HERE = Path("C:/work/trading_bot")
needs_db = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "bot_token": "0:stand", "bingx_trading_mode": "demo", "database_url": _TEST_DB,
        "trading_execution_enabled": True,
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _check(settings: Settings, *, script: Path | PurePosixPath = _HERE / "scripts/miniapp_dev.py",
           cwd: Path | PurePosixPath = _HERE) -> None:
    stand.check_environment(settings, script=script, cwd=cwd)


def test_check_passes_on_dev_machine() -> None:
    _check(_settings())


def test_refuses_real_bot_token() -> None:
    with pytest.raises(stand.StandRefusedError, match="BOT_TOKEN"):
        _check(_settings(bot_token="8867032918:AAxx"))


@pytest.mark.parametrize("where", ["script", "cwd"])
def test_refuses_under_prod_root(where: str) -> None:
    prod = PurePosixPath("/opt/trading_bot")
    kw = {"script": prod / "scripts/miniapp_dev.py"} if where == "script" else {"cwd": prod}
    with pytest.raises(stand.StandRefusedError, match="/opt/trading_bot"):
        _check(_settings(), **kw)


def test_prod_root_prefix_is_not_a_false_match() -> None:
    _check(_settings(), cwd=PurePosixPath("/opt/trading_bot_copy"))


def test_refuses_live_mode() -> None:
    with pytest.raises(stand.StandRefusedError, match="BINGX_TRADING_MODE"):
        _check(_settings(bingx_trading_mode="live"))


def test_refuses_other_database() -> None:
    url = "postgresql+asyncpg://test:test@localhost:5432/trading_bot"
    with pytest.raises(stand.StandRefusedError, match="trading_bot_test"):
        _check(_settings(database_url=url))


def test_main_exits_2_on_refusal(monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(stand, "get_settings", lambda: _settings(bingx_trading_mode="live"))
    monkeypatch.setattr(stand, "serve", None)   # дошли бы до запуска — упало бы TypeError
    assert stand.main([]) == 2
    assert "Стенд не запущен" in capsys.readouterr().err


def test_listens_only_on_localhost() -> None:
    assert stand.HOST == "127.0.0.1"
    assert stand.server_config(stand.build_app(_state()), 8765).host == "127.0.0.1"
    with pytest.raises(SystemExit):
        stand.parse_args(["--host", "0.0.0.0"])


def test_headers_match_plan_csp() -> None:
    assert stand.CSP == (
        "default-src 'self'; script-src 'self' https://telegram.org; connect-src 'self'; "
        "img-src 'self' data:; style-src 'self'; "
        "frame-ancestors https://web.telegram.org https://*.telegram.org"
    )
    assert "unsafe" not in stand.CSP


def test_bypass_lives_only_in_stand() -> None:
    for path in (_ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "dependency_overrides" not in text, path
        assert "miniapp_dev" not in text, path


def test_stand_is_not_in_docker_image() -> None:
    lines = (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "scripts/miniapp_dev.py" in lines


def _state(db: Database | None = None, settings: Settings | None = None) -> ApiState:
    settings = settings or _settings()
    return ApiState(settings=settings, db=db or Database(settings),
                    redis=FakeRedis(decode_responses=True),
                    factory=stand.DevFactory(stand.DevExchange()))


async def _stand_user_id(db: Database) -> int | None:
    async with db.session() as session:
        user = await UserRepository(session).get_by_telegram_id(stand.DEV_TELEGRAM_ID)
        return None if user is None else user.id


@needs_db
async def test_stand_serves_page_and_api_with_headers(unique_telegram_id) -> None:  # type: ignore[no-untyped-def]
    """Свой telegram_id: пользователь запущенного стенда (434343) переживает прогон."""
    settings = _settings(database_url=os.environ["DATABASE_URL"])
    db = Database(settings)
    tid = unique_telegram_id()
    stand_before = await _stand_user_id(db)
    try:
        await stand.ensure_dev_user(db, settings, tid)
        app = stand.build_app(_state(db, settings), tid)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            page = await client.get("/")
            assert page.status_code == 200
            assert "<title>Открыть сделку</title>" in page.text
            for name, value in stand.SECURITY_HEADERS.items():
                assert page.headers[name] == value
            js = await client.get("/app.js")
            assert js.headers["content-type"].startswith(("text/javascript",
                                                          "application/javascript"))
            assert js.headers["Content-Security-Policy"] == stand.CSP
            # Без Authorization: авторизацию подменяет стенд.
            me = await client.get("/api/me")
            assert me.status_code == 200, me.text
            assert me.json()["account_mode"] == "DEMO"
            assert me.headers["Content-Security-Policy"] == stand.CSP
            candles = await client.get("/api/market/XRP-USDT/candles?interval=5m")
            assert candles.status_code == 200, candles.text
            assert len(candles.json()["candles"]) > 100
            # Подмена авторизации отдаёт пользователя с id теста, не стенда.
            async with db.session() as session:
                me_user = await app.dependency_overrides[current_user](session)
            assert me_user.telegram_id == tid
    finally:
        await stand.remove_dev_user(db, tid)
        async with db.session() as session:
            assert await UserRepository(session).get_by_telegram_id(tid) is None
        assert await _stand_user_id(db) == stand_before
        await db.dispose()


def test_tests_never_touch_stand_user() -> None:
    """Тесты не создают и не удаляют пользователя стенда (434343): каждый вызов
    ensure_dev_user/remove_dev_user в tests/ — со своим telegram_id."""
    arity = {"ensure_dev_user": 3, "remove_dev_user": 2}
    calls = 0
    for path in (_ROOT / "tests").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"stand\.(ensure_dev_user|remove_dev_user)\(([^()]*)\)", text):
            calls += 1
            assert len(match.group(2).split(",")) == arity[match.group(1)], (
                path.name, match.group(0))
    assert calls >= 2


@needs_db
async def test_reset_gives_clean_stand(unique_telegram_id) -> None:  # type: ignore[no-untyped-def]
    """POST /dev/reset (сценарии --scenario): пользователь стенда заново, новая фейковая
    биржа, пустой кэш — позиция прошлого сценария не даёт BLOCK следующему."""
    settings = _settings(database_url=os.environ["DATABASE_URL"])
    db = Database(settings)
    tid = unique_telegram_id()
    stand_before = await _stand_user_id(db)
    try:
        old_id = await stand.ensure_dev_user(db, settings, tid)
        state = _state(db, settings)
        old_exchange = state.factory.exchange
        state.market_cache.set("api:price:XRP-USDT", 1, 60)
        app = stand.build_app(state, tid)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.post("/dev/reset")
            assert r.status_code == 200 and r.json() == {"ok": True}
            assert (await client.get("/api/me")).status_code == 200
        async with db.session() as session:
            user = await UserRepository(session).get_by_telegram_id(tid)
        assert user is not None and user.id != old_id
        assert state.factory.exchange is not old_exchange
        assert state.market_cache.size == 0
    finally:
        await stand.remove_dev_user(db, tid)
        assert await _stand_user_id(db) == stand_before
        await db.dispose()


def test_reset_lives_only_in_stand() -> None:
    for path in (_ROOT / "app").rglob("*.py"):
        assert "/dev/reset" not in path.read_text(encoding="utf-8"), path


def test_reset_route_only_in_stand_app() -> None:
    """/dev/reset — маршрут стенда (build_app), у приложения api (create_app) его нет."""
    from app.api.main import create_app

    def paths(app: object) -> set[str]:
        return {getattr(r, "path", "") for r in getattr(app, "routes", [])}

    assert "/dev/reset" not in paths(create_app(_state()))
    assert not any(p.startswith("/dev") for p in paths(create_app(_state())))
    assert "/dev/reset" in paths(stand.build_app(_state()))


@pytest.mark.parametrize(("kw", "where", "fragment"), [
    ({"bot_token": "8867032918:AAxx"}, {}, "BOT_TOKEN"),
    ({"bingx_trading_mode": "live"}, {}, "BINGX_TRADING_MODE"),
    ({"database_url": "postgresql+asyncpg://test:test@localhost:5432/trading_bot"}, {},
     "trading_bot_test"),
    ({}, {"cwd": PurePosixPath("/opt/trading_bot")}, "/opt/trading_bot"),
])
def test_refusals_hold_without_dry_run(kw: dict[str, object], where: dict[str, PurePosixPath],
                                       fragment: str) -> None:
    """Сценарии 2.5 запускают стенд с EXEC_OPEN_DRY_RUN=false — страховки те же."""
    settings = _settings(exec_open_dry_run=False, **kw)
    assert settings.exec_open_dry_run is False
    with pytest.raises(stand.StandRefusedError, match=fragment):
        _check(settings, **where)


def test_dev_machine_without_dry_run_passes() -> None:
    _check(_settings(exec_open_dry_run=False))
