"""Подэтап 3 Mini App (docs/miniapp-plan.md §6): deploy/Caddyfile, сервисы api/caddy в
docker-compose.yml, шаблон override с якорем x-exec-env, .dockerignore; процесс API не
опрашивает Telegram (второй getUpdates = Conflict с поллингом бота).

Docker и Caddy на этой машине не запускаются — проверки по тексту и YAML.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from aiogram import Bot
from aiogram.methods import GetMe
from aiogram.methods.base import TelegramMethod
from aiogram.types import User

from app.api import main as api_main
from app.workers.openings import OpeningsWorker
from scripts import miniapp_dev
from tests.api_helpers import api_fixture  # noqa: F401 — фикстура «api»

ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
SITE = "app.postfactumtrade.ru"


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return data


def _caddy_headers() -> dict[str, str]:
    block = re.search(r"\n\theader \{\n(.*?)\n\t\}", CADDYFILE, re.S)
    assert block, "нет блока header сайта"
    out: dict[str, str] = {}
    for line in block.group(1).splitlines():
        m = re.fullmatch(r'\t\t([A-Za-z-]+) "(.*)"', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


# --- Caddyfile -------------------------------------------------------------------------


def test_csp_and_headers_same_as_stand() -> None:
    """Стенд проверяет фронт в Chrome с теми же заголовками, что отдаст Caddy (отложено с 2.2)."""
    headers = _caddy_headers()
    assert headers.pop("Strict-Transport-Security") == "max-age=31536000"   # без includeSubDomains
    assert headers == miniapp_dev.SECURITY_HEADERS
    assert headers["Content-Security-Policy"] == miniapp_dev.CSP


def test_caddy_site_routes_and_server_header() -> None:
    assert re.search(rf"^{re.escape(SITE)} \{{$", CADDYFILE, re.M)
    assert "\t\t-Server\n" in CADDYFILE
    api = re.search(r"handle /api/\* \{(.*?)\n\t\}", CADDYFILE, re.S)
    assert api and "reverse_proxy api:8000" in api.group(1)
    assert "max_size 64KB" in api.group(1)
    assert "root * /srv/miniapp" in CADDYFILE and "file_server" in CADDYFILE
    assert "encode gzip" in CADDYFILE
    assert re.findall(r"^\s*protocols (.+)$", CADDYFILE, re.M) == ["h1 h2"]   # без HTTP/3
    assert '@app not path /vendor/*' in CADDYFILE and 'header @app Cache-Control "no-cache"' \
        in CADDYFILE


# --- docker-compose.yml ----------------------------------------------------------------


def test_only_caddy_publishes_ports() -> None:
    services = _compose()["services"]
    published = {name: s["ports"] for name, s in services.items() if "ports" in s}
    assert published == {"caddy": ["80:80", "443:443"]}
    for name in ("postgres", "redis", "bot", "api"):
        assert "ports" not in services[name] and "network_mode" not in services[name], name


def test_api_same_image_one_worker_no_scheduler() -> None:
    services = _compose()["services"]
    api, bot = services["api"], services["bot"]
    assert "build" not in api and api["image"] == bot["image"] == "trading_bot-bot:latest"
    assert api["pull_policy"] == "never"
    cmd = api["command"]
    assert cmd[:3] == ["uvicorn", "--factory", "app.api.main:create_app"]
    assert cmd[cmd.index("--workers") + 1] == "1" and cmd[cmd.index("--port") + 1] == "8000"
    assert "app.main" not in " ".join(cmd)                    # не точка входа бота (поллинг)
    assert api["env_file"] == ".env" and api["environment"] == bot["environment"]
    assert api["mem_limit"] == "256m"
    assert "/api/health" in " ".join(api["healthcheck"]["test"])
    assert {k: v["condition"] for k, v in api["depends_on"].items()} == {
        "postgres": "service_healthy", "redis": "service_healthy"}
    assert "./logs:/app/logs" in api["volumes"]


def test_caddy_service() -> None:
    compose = _compose()
    caddy = compose["services"]["caddy"]
    assert re.fullmatch(r"caddy:2\.\d+\.\d+-alpine", caddy["image"])     # точная версия
    assert set(caddy["volumes"]) == {
        "./deploy/Caddyfile:/etc/caddy/Caddyfile:ro", "./miniapp:/srv/miniapp:ro",
        "caddy_data:/data", "caddy_config:/config"}
    assert caddy["depends_on"] == {"api": {"condition": "service_started"}}
    assert caddy["mem_limit"] == "96m"
    assert {"caddy_data", "caddy_config", "postgres_data"} <= set(compose["volumes"])


def test_dockerignore_keeps_static_and_deploy_out_of_image() -> None:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in ("miniapp/", "deploy/", ".env", "docker-compose.override.yml"):
        assert entry in lines, entry


# --- шаблон override: общий якорь переменных исполнения ---------------------------------


def test_override_template_shares_exec_env_anchor() -> None:
    text = (ROOT / "deploy" / "override.example.yml").read_text(encoding="utf-8")
    assert text.count("<<: *exec-env") == 2                 # bot и api — один якорь, не копии
    data = yaml.safe_load(text)
    anchor = data["x-exec-env"]
    assert anchor["EXEC_OPEN_DRY_RUN"] == "true"              # деплой Mini App — сухой прогон
    assert anchor["TRADING_EXECUTION_ENABLED"] == "true"
    bot, api = data["services"]["bot"]["environment"], data["services"]["api"]["environment"]
    for key, value in anchor.items():
        assert bot[key] == api[key] == value, key
    assert set(bot) - set(anchor) == {"MINIAPP_URL"}
    assert set(api) - set(anchor) == {"MINIAPP_ALLOWED_TELEGRAM_IDS"}
    assert api["MINIAPP_ALLOWED_TELEGRAM_IDS"] == ""          # 403 всем до шага 9 деплоя


# --- процесс API не опрашивает Telegram --------------------------------------------------


_CALLS: list[str] = []      # методы Bot API, вызванные процессом API


class _RecordingBot(Bot):
    async def __call__(self, method: TelegramMethod[Any],
                       request_timeout: int | None = None) -> Any:
        _CALLS.append(type(method).__name__)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="bot", username="test_bot")
        raise AssertionError(f"API при старте/остановке вызвал {type(method).__name__}")


async def test_api_lifespan_never_polls_telegram(api, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """Настоящий build_state: за старт и остановку — только getMe; ни getUpdates,
    ни deleteWebhook, ни цикла воркера открытий. В конце остановки — «Остановка
    завершена за X с» (чек-лист деплоя, CLAUDE.md п.8)."""
    _CALLS.clear()
    monkeypatch.setattr(api_main, "Bot", _RecordingBot)
    monkeypatch.setattr(api_main, "get_settings", lambda: api.settings)
    monkeypatch.setattr(api_main, "setup_logging", lambda **kw: None)

    async def no_loop(self: Any) -> None:
        raise AssertionError("API запустил цикл воркера открытий")

    monkeypatch.setattr(OpeningsWorker, "run", no_loop)
    app = api_main.create_app()
    with caplog.at_level(logging.INFO, logger="app.core.shutdown"):
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.api.bot, _RecordingBot)
            assert app.state.api.bot_username == "test_bot"
    assert _CALLS == ["GetMe"]
    assert re.fullmatch(r"Остановка завершена за \d+\.\d с", caplog.messages[-1])


@pytest.mark.parametrize("needle", ["Dispatcher", "start_polling", "get_updates", "GetUpdates",
                                    "delete_webhook", "set_webhook", "app.workers.scheduler"])
def test_api_package_has_no_polling_code(needle: str) -> None:
    for path in (ROOT / "app" / "api").glob("*.py"):
        assert needle not in path.read_text(encoding="utf-8"), f"{path.name}: {needle}"
