"""Кнопка меню бота (подэтап 3, решение владельца 17.10): MINIAPP_URL задан — Mini App,
пусто — список команд, как раньше. Ставится при каждом старте бота и перекрывает
BotFather, поэтому источник один — настройка."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from aiogram.types import MenuButtonCommands, MenuButtonWebApp

from app import main as bot_main
from app.core.config import Settings

URL = "https://app.postfactumtrade.ru/"
BASE: dict[str, Any] = {"bot_token": "123:abc", "database_url": "postgresql+asyncpg://u:p@h/d",
                        "encryption_key": "0" * 43 + "="}


def _settings(**kw: Any) -> Settings:
    return Settings(**BASE, **kw)


class _Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def set_my_commands(self, commands: Any) -> bool:
        self.calls.append(("commands", commands))
        return True

    async def set_chat_menu_button(self, menu_button: Any = None, **kw: Any) -> bool:
        assert not kw, kw                         # без chat_id — кнопка по умолчанию для всех
        self.calls.append(("menu", menu_button))
        return True


def test_empty_url_keeps_commands_button() -> None:
    assert _settings().miniapp_url == ""
    assert isinstance(bot_main.menu_button(_settings()), MenuButtonCommands)


def test_url_gives_web_app_button() -> None:
    button = bot_main.menu_button(_settings(miniapp_url=URL))
    assert isinstance(button, MenuButtonWebApp)
    assert button.text == bot_main.MINIAPP_BUTTON_TEXT == "Торговля"
    assert button.web_app.url == URL


@pytest.mark.parametrize("url", ["http://app.postfactumtrade.ru/", "https://",
                                 "app.postfactumtrade.ru", "https://app.postfactumtrade.ru/ x",
                                 "javascript:alert(1)"])
def test_bad_url_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="MINIAPP_URL"):
        _settings(miniapp_url=url)


def test_url_trimmed() -> None:
    assert _settings(miniapp_url=f"  {URL} ").miniapp_url == URL
    assert _settings(miniapp_url="   ").miniapp_url == ""


@pytest.mark.parametrize(("url", "kind"), [("", MenuButtonCommands), (URL, MenuButtonWebApp)])
async def test_setup_bot_ui_sets_button_from_settings(url: str, kind: type) -> None:
    bot = _Bot()
    await bot_main.setup_bot_ui(bot, _settings(miniapp_url=url))  # type: ignore[arg-type]
    assert [name for name, _ in bot.calls] == ["commands", "menu"]
    assert isinstance(bot.calls[1][1], kind)


def test_menu_button_set_only_in_setup_bot_ui() -> None:
    """Других мест, где кнопку меню ставит код, нет — иначе источников два."""
    app = Path(__file__).resolve().parents[1] / "app"
    found = [p.relative_to(app).as_posix() for p in app.rglob("*.py")
             if re.search(r"set_chat_menu_button\(", p.read_text(encoding="utf-8"))]
    assert found == ["main.py"]
    main = (app / "main.py").read_text(encoding="utf-8")
    assert main.count("set_chat_menu_button(") == 1
    assert "await setup_bot_ui(bot, settings)" in main
