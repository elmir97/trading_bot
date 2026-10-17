"""Старт Mini App без SDK telegram.org (2а, решение владельца 17.10) — по тексту
`miniapp/index.html`, `boot.js`, `app.js` (JS-раннера нет; в браузере — сценарии
«SDK заблокирован / завис / в норме» в headless Chrome, см. handoff):

- разбор страницы SDK не держит: SDK и скрипты страницы — defer, до них только
  синхронный `boot.js`; пока грузится — статичное «Загрузка…» из HTML;
- через 10 с без `Telegram.WebApp` — экран «Не загрузилось…» с «Обновить»;
- SDK упал — `app.js` показывает тот же экран сразу; запасного режима без SDK нет.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

_MINIAPP = Path(__file__).resolve().parents[1] / "miniapp"
_SDK = "https://telegram.org/js/telegram-web-app.js"
SDK_FAILED_TEXT = "Не загрузилось. Проверь интернет и открой заново из бота"


def _text(name: str) -> str:
    return (_MINIAPP / name).read_text(encoding="utf-8")


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.texts: dict[str, str] = {}
        self._open: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script":
            self.scripts.append(values)
        element_id = values.get("id")
        if element_id:
            self.elements[element_id] = (tag, values)
            self.texts[element_id] = ""
        self._open.append(element_id)

    def handle_endtag(self, tag: str) -> None:
        if self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        for element_id in self._open:
            if element_id:
                self.texts[element_id] += data


def _page() -> _Page:
    page = _Page()
    page.feed(_text("index.html"))
    return page


def _body(text: str, start: str) -> str:
    i = text.index("{", text.index(start))
    depth = 0
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i:j + 1]
    raise AssertionError(start)


def test_sdk_does_not_block_parsing() -> None:
    scripts = _page().scripts
    assert [s.get("src") for s in scripts][:2] == ["boot.js", _SDK]
    boot, sdk, *rest = scripts
    # boot.js — синхронный, до SDK: таймер идёт, даже если SDK не ответит.
    assert "defer" not in boot and "async" not in boot and boot.get("type") is None
    assert "defer" in sdk
    for script in rest:                        # библиотека и app.js — после SDK, не блокируют
        assert "defer" in script or script.get("type") == "module", script


def test_static_loading_and_sdk_failed_screens() -> None:
    page = _page()
    _, loading = page.elements["screen-loading"]
    assert "hidden" not in loading
    assert "Загрузка…" in page.texts["screen-loading"]
    tag, sdk = page.elements["screen-sdk"]
    assert tag == "section" and "hidden" in sdk and sdk.get("class") == "screen"
    assert SDK_FAILED_TEXT in " ".join(page.texts["screen-sdk"].split())
    tag, _ = page.elements["sdk-reload"]
    assert tag == "button" and page.texts["sdk-reload"].strip() == "Обновить"


def test_boot_timeout_shows_sdk_failed_screen() -> None:
    boot = _text("boot.js")
    assert re.search(r"var SDK_TIMEOUT_MS = 10000;", boot)
    timer = boot[boot.index("setTimeout(function () {"):]
    assert "if (!hasSdk()) fail();" in timer
    assert "}, SDK_TIMEOUT_MS);" in timer
    assert "window.Telegram && window.Telegram.WebApp" in _body(boot, "function hasSdk()")
    fail = _body(boot, "function fail()")
    assert "if (failed) return;" in fail
    # До разбора <body> экрана ещё нет — показать по readystatechange.
    assert 'document.readyState === "loading"' in fail
    assert 'addEventListener("readystatechange", show, { once: true })' in fail
    show = _body(boot, "function show()")
    assert 'screens[i].hidden = screens[i].id !== "screen-sdk";' in show
    assert 'document.querySelectorAll("main > .screen")' in show
    assert "window.location.reload()" in show
    assert 'getElementById("sdk-reload").addEventListener("click"' in show


def test_sdk_load_error_shows_screen_at_once() -> None:
    """Ошибка загрузки SDK — экран сразу, не через 10 с: слушатель error в фазе
    перехвата (ошибка <script> не всплывает), не on*= в HTML."""
    boot = _text("boot.js")
    assert f'var SDK_URL = "{_SDK}";' in boot
    listener = boot[boot.index('window.addEventListener("error", function (event) {'):]
    listener = listener[:listener.index("}, true);") + len("}, true);")]
    assert 'target.tagName === "SCRIPT" && target.src === SDK_URL) fail();' in listener
    assert "onerror" not in boot and "onerror" not in _text("index.html")


def test_app_without_sdk_shows_same_screen_and_stops() -> None:
    """Запасного режима нет: SDK не загрузился (или таймер уже сработал) — экран
    «Не загрузилось», ни одного запроса к API."""
    app = _text("app.js")
    init = _body(app, "function init()")
    guard = "if (!tg || window.maBoot.failed()) {"
    assert init.index(guard) < init.index("tg.ready()") < init.index("start()")
    branch = _body(init, guard)
    assert "window.maBoot.fail();" in branch and "return;" in branch
    # Каждый экран, который показывает app.js, — из SCREENS; экран SDK — только boot.js.
    assert '"sdk"' not in app


def test_outside_telegram_no_api_and_bot_name() -> None:
    """Safari по ссылке (17.10): SDK есть, initData пустая — сразу «Открой Mini App из бота
    @postfactumTrade_bot», ни экранов, ни запросов к API, ни «Повторить»."""
    app = _text("app.js")
    init = _body(app, "function init()")
    guard = "if (!tg.initData) {"
    assert init.index("if (!tg || window.maBoot.failed()) {") < init.index(guard)
    order = [init.index(x) for x in (guard, "tg.ready()", "screens = {", "start()")]
    assert order == sorted(order)
    branch = _body(init, guard)
    assert "showOutsideTelegram();" in branch and "return;" in branch
    outside = _body(app, "function showOutsideTelegram()")
    assert 'const BOT_USERNAME = "postfactumTrade_bot";' in app
    assert "Открой Mini App из бота @${BOT_USERNAME}" in outside
    assert '$("error-retry").hidden = true;' in outside
    assert "api." not in outside and "fetch(" not in outside


def test_api_non_json_proxy_errors_get_own_text() -> None:
    """502/503/504 без JSON (Caddy без api) — «Сервер недоступен…»; любой JSON-ответ ошибки
    (401/403/422/503 FastAPI) — текст сервера; прочий не-JSON — «ответил непонятно»."""
    api = _text("api.js")
    assert "const PROXY_STATUSES = [502, 503, 504];" in api
    assert 'export const UNAVAILABLE_TEXT = "Сервер недоступен — повтори через минуту.";' in api
    catch = api[api.index("data = await response.json();"):api.index("if (!response.ok)")]
    assert "PROXY_STATUSES.includes(response.status)" in catch
    assert '"UNAVAILABLE", UNAVAILABLE_TEXT' in catch
    assert '"BAD_RESPONSE", BAD_RESPONSE_TEXT' in catch
    ok = _body(api, "if (!response.ok)")
    assert "data.message" in ok and "data.code" in ok          # JSON-ошибка — текст сервера
