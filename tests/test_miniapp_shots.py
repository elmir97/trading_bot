"""Снимки экранов Mini App (`scripts/miniapp_shots.py`, `miniapp_browser_check --shots`) —
без Chrome: тема при открытии (themeParams в хеше + prefers-color-scheme), снимок страницы
целиком, папка вне репозитория, набор экран × тема × ширина, коды выхода."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from scripts import miniapp_browser_check as bc
from scripts import miniapp_scenarios as ms
from scripts import miniapp_shots as mshots

URL = "http://127.0.0.1:8765/"
PNG = b"\x89PNG\r\n\x1a\nfake"


class Browser:
    """Ответ на каждую команду по id; снимок — PNG, высота страницы — `content`."""

    def __init__(self, content: float = 1500.5) -> None:
        self.sent: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.content = content

    async def send(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)
        result: dict[str, Any] = {}
        if msg["method"] == "Page.getLayoutMetrics":
            result = {"cssContentSize": {"x": 0, "y": 0, "width": 390, "height": self.content}}
        elif msg["method"] == "Page.captureScreenshot":
            result = {"data": base64.b64encode(PNG).decode()}
        self.queue.put_nowait({"id": msg["id"], "result": result})

    async def receive(self) -> dict[str, Any]:
        return await self.queue.get()

    def calls(self, method: str) -> list[dict[str, Any]]:
        return [m["params"] for m in self.sent if m["method"] == method]


def _theme_in_hash(url: str) -> dict[str, str]:
    raw = parse_qs(urlsplit(url).fragment)["tgWebAppThemeParams"][0]
    return json.loads(unquote(raw))  # type: ignore[no-any-return]


# --- тема при открытии -------------------------------------------------------------------


def test_page_url_carries_theme_params() -> None:
    url = bc.page_url(URL, theme_params=ms.THEME_PARAMS["dark"])
    assert _theme_in_hash(url) == ms.THEME_PARAMS["dark"]
    assert bc.page_url(URL) == bc.page_url(URL, theme_params=None)   # без темы — как раньше


def test_theme_params_are_light_and_dark_by_bg() -> None:
    """SDK решает colorScheme по яркости bg_color: светлая — светлый фон, тёмная — тёмный."""
    def luma(hex_: str) -> float:
        r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
        return 0.299 * r + 0.587 * g + 0.114 * b
    assert set(ms.THEME_PARAMS) == set(mshots.THEMES) == {"light", "dark"}
    assert luma(ms.THEME_PARAMS["light"]["bg_color"]) > 200
    assert luma(ms.THEME_PARAMS["dark"]["bg_color"]) < 60


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_open_page_sets_theme_before_navigate(theme: str) -> None:
    t = Browser()
    await ms.open_page(ms.Page(t), URL, ms.Setup(theme=theme, wait_screen=None))
    methods = [m["method"] for m in t.sent]
    assert methods.index("Emulation.setEmulatedMedia") < methods.index("Page.navigate")
    assert t.calls("Emulation.setEmulatedMedia") == [
        {"features": [{"name": "prefers-color-scheme", "value": theme}]}]
    assert _theme_in_hash(t.calls("Page.navigate")[0]["url"]) == ms.THEME_PARAMS[theme]


async def test_open_page_without_theme_emulates_nothing() -> None:
    t = Browser()
    await ms.open_page(ms.Page(t), URL, ms.Setup(wait_screen=None))
    assert t.calls("Emulation.setEmulatedMedia") == []
    assert "tgWebAppThemeParams" not in t.calls("Page.navigate")[0]["url"]


# --- снимок ------------------------------------------------------------------------------


@pytest.mark.parametrize(("content", "height"), [(1500.5, 1501), (600, 844)])
async def test_capture_whole_page_at_width_and_scale(tmp_path: Path, content: float,
                                                     height: int) -> None:
    t = Browser(content)
    path = tmp_path / "x.png"
    await mshots.capture(ms.Page(t), path, 320)
    assert t.calls("Emulation.setDeviceMetricsOverride")[-1] == {
        "width": 320, "height": 844, "deviceScaleFactor": 2, "mobile": True}
    assert t.calls("Page.captureScreenshot") == [{
        "format": "png", "captureBeyondViewport": True,
        "clip": {"x": 0, "y": 0, "width": 320, "height": height, "scale": 1}}]
    assert path.read_bytes() == PNG


async def test_run_shot_saves_every_width_with_theme(tmp_path: Path) -> None:
    t = Browser()
    shot = mshots.Shot("market", "рынок", ms.Setup(wait_screen=None))
    page, saved = await mshots.run_shot(t, URL, shot, "dark", tmp_path)
    assert [p.name for p in saved] == ["market-dark-390.png", "market-dark-320.png"]
    assert all(p.read_bytes() == PNG for p in saved)
    assert page.problems == []
    assert _theme_in_hash(t.calls("Page.navigate")[0]["url"]) == ms.THEME_PARAMS["dark"]


async def test_run_shot_stops_on_scenario_error(tmp_path: Path) -> None:
    async def broken(page: ms.Page) -> None:
        raise ms.ScenarioError("нет формы")
    shot = mshots.Shot("form", "форма", ms.Setup(wait_screen=None), broken)
    page, saved = await mshots.run_shot(Browser(), URL, shot, "light", tmp_path)
    assert saved == []
    assert page.issues() == ["снимок прерван: нет формы"]
    assert list(tmp_path.iterdir()) == []


# --- набор и папка -------------------------------------------------------------------------


def test_shots_cover_screens_1_to_4() -> None:
    assert list(mshots.SHOTS) == ["market", "form", "confirm", "positions"]
    assert mshots.WIDTHS == (390, 320)
    assert [s.name for s in mshots.select("all")] == list(mshots.SHOTS)
    with pytest.raises(bc.EnvironmentNotReadyError, match="нет снимка"):
        mshots.select("nope")


def test_shot_presets_exist_on_stand() -> None:
    from scripts import miniapp_dev

    used = {s.setup.preset for s in mshots.SHOTS.values() if s.setup.preset}
    assert used and used <= set(miniapp_dev.PRESETS)


def test_shots_use_own_buttons_not_native() -> None:
    """Нативный MainButton рисует Telegram — на снимке страницы его не было бы."""
    assert all(not s.setup.native for s in mshots.SHOTS.values())


def test_out_dir_refuses_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    for inside in (repo, repo / "docs", repo / "new" / "shots"):
        with pytest.raises(bc.EnvironmentNotReadyError, match="вне репозитория"):
            mshots.out_dir(str(inside), repo)
    outside = mshots.out_dir(str(tmp_path / "shots" / "before"), repo)
    assert outside.is_dir()
    assert not (repo / "new").exists()


def test_out_dir_default_repo_is_this_checkout() -> None:
    assert (mshots.REPO / "scripts" / "miniapp_shots.py").is_file()
    with pytest.raises(bc.EnvironmentNotReadyError):
        mshots.out_dir(str(mshots.REPO / "shots"))


# --- запуск --------------------------------------------------------------------------------


@pytest.fixture
def chrome_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "chrome-stub"
    exe.write_bytes(b"")
    return exe


def _page(*problems: str) -> ms.Page:
    page = ms.Page(Browser())
    page.problems.extend(problems)
    return page


@pytest.mark.parametrize(("problems", "widths", "code"), [
    ((), 2, 0), (("плохо",), 2, 1), ((), 1, 1)])
def test_main_shots_exit_codes(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                               tmp_path: Path, capsys: pytest.CaptureFixture[str],
                               problems: tuple[str, ...], widths: int, code: int) -> None:
    seen: list[tuple[list[str], Path]] = []

    async def fake(url: str, chrome: Path, items: list[mshots.Shot],
                   directory: Path) -> list[Any]:
        seen.append(([i.name for i in items], directory))
        return [(item, theme, _page(*problems), [directory / "x.png"] * widths)
                for item in items for theme in mshots.THEMES]

    monkeypatch.setattr(bc, "check_shots", fake)
    monkeypatch.setattr(bc, "check", None)
    monkeypatch.setattr(bc, "check_scenarios", None)
    shots = tmp_path / "shots"
    assert bc.main(["--chrome", str(chrome_exe), "--shots", str(shots), "--shot", "form"]) == code
    assert seen == [(["form"], shots.resolve())]
    out = capsys.readouterr().out
    assert f"снимков {2 * widths} в {shots.resolve()}" in out
    if code:
        assert "form-light" in out and "form-dark" in out


@pytest.mark.parametrize("argv", [
    ["--shot", "nope"],
    ["--scenario", "poll"],
])
def test_main_shots_refusals_are_exit_2(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                                        tmp_path: Path, argv: list[str]) -> None:
    monkeypatch.setattr(bc, "check_shots", None)
    monkeypatch.setattr(bc, "check_scenarios", None)
    assert bc.main(["--chrome", str(chrome_exe), "--shots", str(tmp_path / "s"), *argv]) == 2


def test_main_shots_inside_repo_is_exit_2(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(bc, "check_shots", None)
    assert bc.main(["--chrome", str(chrome_exe), "--shots", str(mshots.REPO / "shots")]) == 2
    assert "вне репозитория" in capsys.readouterr().err
    assert not (mshots.REPO / "shots").exists()


def test_shots_not_in_docker_image() -> None:
    ignored = (mshots.REPO / ".dockerignore").read_text(encoding="utf-8").split()
    assert "scripts/miniapp_shots.py" in ignored


def test_module_constants_assigned_once() -> None:
    text = Path(mshots.__file__).read_text(encoding="utf-8")
    names = re.findall(r"^([A-Z_]+)\s*[:=]", text, flags=re.MULTILINE)
    assert len(names) == len(set(names))
