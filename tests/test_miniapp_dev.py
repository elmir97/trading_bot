"""Локальный стенд фронта `scripts/miniapp_dev.py` (docs/miniapp-plan.md §9а):
страховки от прода, только 127.0.0.1, заголовки как у Caddy, обход авторизации
— только в стенде."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from app.api.deps import ApiState
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


@needs_db
async def test_stand_serves_page_and_api_with_headers() -> None:
    settings = _settings(database_url=os.environ["DATABASE_URL"])
    db = Database(settings)
    try:
        await stand.ensure_dev_user(db, settings)
        app = stand.build_app(_state(db, settings))
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
    finally:
        await stand.remove_dev_user(db)
        async with db.session() as session:
            assert await UserRepository(session).get_by_telegram_id(stand.DEV_TELEGRAM_ID) is None
        await db.dispose()
