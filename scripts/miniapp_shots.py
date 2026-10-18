"""Снимки экранов Mini App на стенде: `miniapp_browser_check --shots DIR [--shot <имя>|all]`.

Подэтап «Дизайн Mini App» (§9а плана): снимки «до» и «после» одним набором — экраны
1–4 (рынок, форма, подтверждение, позиции) × светлая и тёмная тема × 390 и 320 px.
Файл — `<экран>-<тема>-<ширина>.png`, страница целиком (не только окно), масштаб 2.

Каждый экран в каждой теме — свой свежий Chrome (как сценарии): тема задаётся при
открытии (themeParams клиента в хеше + prefers-color-scheme), а не событием после.
Кнопки — свои (браузер 6.0): нативный MainButton рисует Telegram, на снимке его нет.

DIR — вне репозитория: снимки в git не попадают (отказ, exit 2). Замечания (CSP,
исключения, HTTP ≥ 400, не дождался экрана) — exit 1, как у сценариев.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from scripts import miniapp_browser_check as bc
from scripts import miniapp_scenarios as ms

REPO = Path(__file__).resolve().parents[1]
THEMES = ("light", "dark")
WIDTHS = (390, 320)
HEIGHT = 844
SCALE = 2
SETTLE = 1.0                     # после смены ширины: график (autoSize) и шрифты


@dataclass(frozen=True)
class Shot:
    name: str
    title: str
    setup: ms.Setup
    go: Callable[[ms.Page], Awaitable[object]] | None = None


async def _form(page: ms.Page) -> None:
    await ms._to_form(page, False)
    await page.click('#trade-stop-chips [data-percent="2"]')
    await ms._settled(page, "превью формы")


async def _confirm(page: ms.Page) -> None:
    await _form(page)
    await ms._to_confirm(page, False)


SHOTS: dict[str, Shot] = {s.name: s for s in (
    Shot("market", "1 Рынок: XRP, позиция (линии и блок «Позиция»)", ms.Setup(preset="single")),
    Shot("form", "2 Форма: стоп 2 %, превью", ms.Setup(), _form),
    Shot("confirm", "3 Подтверждение: карточка", ms.Setup(), _confirm),
    Shot("positions", "4 Позиции: хедж, лестница стопов, без стопа", ms.Setup(preset="hedge"),
         ms._to_positions),
)}


def select(name: str) -> list[Shot]:
    if name == "all":
        return list(SHOTS.values())
    if name not in SHOTS:
        raise bc.EnvironmentNotReadyError(f"нет снимка {name!r}; есть: all, {', '.join(SHOTS)}")
    return [SHOTS[name]]


def out_dir(raw: str, repo: Path = REPO) -> Path:
    """Папка снимков: создаётся; внутри репозитория — отказ (PNG не для git)."""
    path = Path(raw).resolve()
    if path == repo or repo in path.parents:
        raise bc.EnvironmentNotReadyError(f"снимки — вне репозитория: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_name(shot: str, theme: str, width: int) -> str:
    return f"{shot}-{theme}-{width}.png"


async def capture(page: ms.Page, path: Path, width: int) -> None:
    """Страница целиком при ширине `width` (масштаб SCALE) → PNG."""
    await page.call("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": HEIGHT, "deviceScaleFactor": SCALE, "mobile": True})
    await page.pump(SETTLE)
    metrics = await page.call("Page.getLayoutMetrics")
    height = max(HEIGHT, math.ceil(metrics.get("cssContentSize", {}).get("height", 0)))
    shot = await page.call("Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": True,
        "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1}})
    path.write_bytes(base64.b64decode(shot["data"]))


async def run_shot(transport: bc.Transport, url: str, shot: Shot, theme: str, directory: Path,
                   reset: Callable[[], Awaitable[None]] | None = None,
                   seed: Callable[[str], Awaitable[None]] | None = None,
                   ) -> tuple[ms.Page, list[Path]]:
    page = ms.Page(transport)
    page.reset_stand = reset
    page.seed_stand = seed
    saved: list[Path] = []
    try:
        await ms.open_page(page, url, replace(shot.setup, theme=theme))
        if shot.go is not None:
            await shot.go(page)
        for width in WIDTHS:
            path = directory / file_name(shot.name, theme, width)
            await capture(page, path, width)
            saved.append(path)
    except ms.ScenarioError as exc:
        page.problems.append(f"снимок прерван: {exc}")
    return page, saved


def render(shot: Shot, theme: str, page: ms.Page, saved: list[Path]) -> str:
    issues = page.issues()
    head = f"{'✗' if issues else '✓'} {shot.name}-{theme} — {shot.title}"
    return "\n".join([head] + [f"    · {p.name}" for p in saved]
                     + [f"    ✗ {i}" for i in issues])


__all__ = ["SHOTS", "THEMES", "WIDTHS", "Shot", "capture", "file_name", "out_dir", "render",
           "run_shot", "select"]
