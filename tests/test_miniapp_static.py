"""Статика Mini App не ломает CSP Caddy (docs/miniapp-plan.md §5, §5а, §8).

CSP: `script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'`
без 'unsafe-inline'/'unsafe-eval'. Поэтому по тексту файлов `miniapp/` (кроме
`vendor/` — его держит test_miniapp_vendor.py по sha256):
- HTML подключает только свои файлы и SDK telegram.org; инлайн-скриптов,
  `<style>`, атрибутов `style=` и `on*=` нет;
- свой JS без eval/Function/строкового setTimeout, без `innerHTML` и других
  вставок HTML (серверные строки — только textContent), без инлайн-стилей
  через `setAttribute("style")`/`cssText`; внешних адресов, кроме ссылки
  атрибуции TradingView, нет;
- атрибуция TradingView — своя ссылка через `Telegram.WebApp.openLink`, без
  referrer; встроенный логотип (вставляет `<style>`) выключен у каждого графика.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

_MINIAPP = Path(__file__).resolve().parents[1] / "miniapp"
_TELEGRAM_SDK = "https://telegram.org/js/telegram-web-app.js"
_TV_URL = "https://www.tradingview.com/"
_LIBRARY = "vendor/lightweight-charts-5.2.1.js"


def _own(pattern: str) -> list[Path]:
    found = _MINIAPP.rglob(pattern)
    return sorted(p for p in found if "vendor" not in p.relative_to(_MINIAPP).parts)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str | None]] = []
        self.links: list[dict[str, str | None]] = []
        self.anchors: list[dict[str, str | None]] = []
        self.inline_scripts: list[str] = []
        self.style_tags = 0
        self.bad_attrs: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for name, _ in attrs:
            if name == "style" or name.startswith("on"):
                self.bad_attrs.append(f"<{tag} {name}=>")
        if tag == "script":
            self.scripts.append(values)
            self._in_script = True
        elif tag == "style":
            self.style_tags += 1
        elif tag == "link":
            self.links.append(values)
        elif tag == "a":
            self.anchors.append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_scripts.append(data.strip()[:60])


def _parse(path: Path) -> _Page:
    page = _Page()
    page.feed(path.read_text(encoding="utf-8"))
    return page


def _local(ref: str) -> bool:
    return not re.match(r"^[a-z][a-z0-9+.-]*:|^//", ref, re.IGNORECASE)


def test_pages_exist() -> None:
    assert (_MINIAPP / "index.html").is_file()
    assert _own("*.html") == [_MINIAPP / "index.html"]


@pytest.mark.parametrize("page_path", _own("*.html"), ids=lambda p: p.name)
def test_html_has_no_inline_code_or_styles(page_path: Path) -> None:
    page = _parse(page_path)
    assert page.inline_scripts == []
    assert page.style_tags == 0
    assert page.bad_attrs == []


@pytest.mark.parametrize("page_path", _own("*.html"), ids=lambda p: p.name)
def test_html_loads_only_own_files_and_telegram(page_path: Path) -> None:
    page = _parse(page_path)
    for script in page.scripts:
        src = script.get("src")
        assert src, "скрипт без src — инлайн"
        if not _local(src):
            assert src == _TELEGRAM_SDK, src
        else:
            assert (_MINIAPP / src).is_file(), src
    for link in page.links:
        href = link.get("href") or ""
        assert _local(href), href
        assert (_MINIAPP / href).is_file(), href


def test_index_loads_sdk_library_and_app_module() -> None:
    srcs = [s.get("src") for s in _parse(_MINIAPP / "index.html").scripts]
    assert srcs == [_TELEGRAM_SDK, _LIBRARY, "app.js"]
    app = [s for s in _parse(_MINIAPP / "index.html").scripts if s.get("src") == "app.js"]
    assert app[0].get("type") == "module"


_FORBIDDEN_JS = {
    "eval": re.compile(r"\beval\s*\("),
    "Function": re.compile(r"\bFunction\s*\("),
    "строковый таймер": re.compile(r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]"),
    "вставка HTML": re.compile(r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b"),
    "инлайн-стиль": re.compile(r"setAttribute\(\s*[\"']style|\bcssText\b"),
    "атрибут on*": re.compile(r"setAttribute\(\s*[\"']on"),
}


@pytest.mark.parametrize("js", _own("*.js"), ids=lambda p: p.name)
def test_own_js_is_csp_safe(js: Path) -> None:
    text = js.read_text(encoding="utf-8")
    found = [name for name, rx in _FORBIDDEN_JS.items() if rx.search(text)]
    assert found == []


@pytest.mark.parametrize("js", _own("*.js"), ids=lambda p: p.name)
def test_own_js_has_no_external_addresses(js: Path) -> None:
    urls = set(re.findall(r"(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s\"'`)]*",
                          js.read_text(encoding="utf-8")))
    assert urls <= {_TV_URL}, urls


@pytest.mark.parametrize("js", _own("*.js"), ids=lambda p: p.name)
def test_own_js_imports_exist(js: Path) -> None:
    text = js.read_text(encoding="utf-8")
    for ref in re.findall(r"""\bfrom\s+["']([^"']+)["']|\bimport\s*\(\s*["']([^"']+)["']""", text):
        target = next(r for r in ref if r)
        assert target.startswith("./"), target
        assert (js.parent / target).resolve().is_file(), target


def test_css_has_no_external_resources() -> None:
    for css in _own("*.css"):
        text = css.read_text(encoding="utf-8")
        assert "@import" not in text
        assert re.search(r"url\(\s*[\"']?(?:https?:)?//", text) is None, css.name


def test_tradingview_attribution_is_own_link_without_referrer() -> None:
    anchors = [a for a in _parse(_MINIAPP / "index.html").anchors if a.get("href") == _TV_URL]
    assert len(anchors) == 1
    rel = (anchors[0].get("rel") or "").split()
    assert {"noopener", "noreferrer"} <= set(rel)
    app = (_MINIAPP / "app.js").read_text(encoding="utf-8")
    assert "openLink(" in app
    assert '"noopener,noreferrer"' in app


def test_every_chart_disables_builtin_logo() -> None:
    """Встроенный логотип вставляет <style> — style-src 'self' его режет (§5а)."""
    for js in _own("*.js"):
        text = js.read_text(encoding="utf-8")
        charts = len(re.findall(r"\bcreateChart\s*\(", text))
        assert len(re.findall(r"attributionLogo\s*:\s*false", text)) >= charts, js.name
        assert re.search(r"attributionLogo\s*:\s*true", text) is None, js.name


def test_storage_never_keeps_init_data() -> None:
    storage = (_MINIAPP / "storage.js").read_text(encoding="utf-8")
    assert "initData" not in storage.replace("initDataUnsafe", "")
    for js in _own("*.js"):
        text = js.read_text(encoding="utf-8")
        assert re.search(r"\.save\([^)]*initData\b", text) is None, js.name
