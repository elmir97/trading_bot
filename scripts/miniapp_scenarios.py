"""Сценарии браузера Mini App на стенде: `miniapp_browser_check --scenario <имя>|all`.

Каждый сценарий — свой свежий Chrome (headless, временный профиль, только 127.0.0.1)
и страница стенда; управление — DevTools-протокол:
- действия — клики и ввод из JS страницы, события Telegram через
  `Telegram.WebView.receiveEvent` (кнопки, «Назад», тема), версия клиента —
  `#tgWebAppVersion=8.0` (нативные MainButton/SecondaryButton);
- сеть — `Fetch`: правило (Rule) по подстроке адреса отвечает своим статусом,
  правит JSON ответа сервера, обрывает запрос до сервера или держит его без ответа;
- проверка — состояние страницы (STATE_JS) и журнал запросов к /api (метод, путь, тело).

Перед каждым сценарием стенд сбрасывается (`POST /dev/reset` стенда): новая фейковая
биржа и пользователь стенда без сделок — сценарии не зависят от порядка. Позиции
(«Позиции», линии) — `POST /dev/positions?preset=…` стенда (Setup.preset).

Замечания сценария = невыполненные ожидания + замечания Collector (CSP, исключения,
console, HTTP ≥ 400, сбои загрузки); статусы и сбои, вызванные своими правилами,
замечанием не считаются. Ничего не уходит на биржу: стенд — фейковая биржа.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from scripts import miniapp_browser_check as bc

SYMBOL = "XRP-USDT"
NATIVE_HASH = "#tgWebAppVersion=8.0"
CALL_TIMEOUT = 10.0
POLL_STEP = 0.2
STORAGE = "ma:anon:"             # storage.js вне Telegram (как bc.STORAGE_SYMBOL_KEY)

STATE_JS = """(() => {
  const $ = (id) => document.getElementById(id);
  const shown = (id) => {
    const e = $(id);
    return !!e && !e.hidden && !e.closest('[hidden]');
  };
  const text = (id) => {
    const e = $(id);
    return e ? e.textContent.replace(/\\s+/g, ' ').trim() : null;
  };
  const tg = window.Telegram && window.Telegram.WebApp;
  const box = $('market-chart');
  const chart = box && box.querySelector('.tv-lightweight-charts');
  return {
    screens: [...document.querySelectorAll('main > .screen')]
      .filter((s) => !s.hidden).map((s) => s.id),
    charts: document.querySelectorAll('.tv-lightweight-charts').length,
    market_charts: document.querySelectorAll('#market-chart .tv-lightweight-charts').length,
    trade_charts: document.querySelectorAll('#trade-chart .tv-lightweight-charts').length,
    symbol: text('market-symbol'),
    last: text('market-last'),
    interval_label: text('market-interval-label'),
    stale: shown('market-stale') ? text('market-stale') : null,
    market_error: shown('market-error') ? text('market-error-text') : null,
    chart_state: shown('market-chart-state') ? text('market-chart-text') : null,
    search: shown('market-search'),
    search_items: [...document.querySelectorAll('#market-search-list [data-symbol]')]
      .map((b) => b.dataset.symbol),
    error_title: shown('screen-error') ? text('error-title') : null,
    sdk_text: shown('screen-sdk') ? text('screen-sdk') : null,
    trade_title: text('trade-title'),
    blocked: shown('trade-blocked') ? text('trade-blocked') : null,
    form_shown: shown('trade-form'),
    lev: $('trade-leverage') ? $('trade-leverage').value : null,
    lev_max: $('trade-leverage') ? $('trade-leverage').max : null,
    take: $('trade-take') ? $('trade-take').value : null,
    take_2r_disabled: $('trade-take-2r') ? $('trade-take-2r').disabled : null,
    risk_invalid: $('trade-risk') ? $('trade-risk').classList.contains('invalid') : null,
    result: shown('trade-result') ? text('trade-result') : null,
    pending: $('trade-result') ? $('trade-result').classList.contains('pending') : null,
    preview_state: shown('trade-preview-state') ? text('trade-preview-state') : null,
    fallback: shown('fallback-buttons') ? text('fallback-main') : null,
    main_button: tg && tg.MainButton && tg.MainButton.isVisible ? tg.MainButton.text : null,
    main_active: tg && tg.MainButton ? tg.MainButton.isActive : null,
    confirm_live: shown('confirm-live'),
    confirm_title: text('confirm-title'),
    confirm_card: shown('screen-confirm') ? $('confirm-card').textContent : null,
    confirm_timer: shown('confirm-timer') ? text('confirm-timer') : null,
    confirm_status: shown('confirm-status') ? $('confirm-status').textContent : null,
    confirm_diff: shown('confirm-diff') ? text('confirm-diff') : null,
    confirm_action: shown('confirm-action') && !$('confirm-action').disabled
      ? text('confirm-action') : null,
    confirm_back: shown('confirm-back') ? text('confirm-back') : null,
    stop: $('trade-stop') ? $('trade-stop').value : null,
    scroll_x: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    chart_box_width: box ? Math.round(box.getBoundingClientRect().width) : null,
    chart_canvas_width: chart ? Math.round(chart.getBoundingClientRect().width) : null,
    menu_link: shown('account-positions'),
    settings_button: tg && tg.SettingsButton ? tg.SettingsButton.isVisible : null,
    account_note: shown('account-note') ? text('account-note') : null,
    positions_state: shown('positions-state') ? text('positions-state-text') : null,
    positions_retry: shown('positions-retry'),
    positions_note: shown('positions-note') ? text('positions-note') : null,
    positions_stale: shown('positions-stale') ? text('positions-stale') : null,
    positions_empty: shown('positions-empty') ? text('positions-empty-text') : null,
    positions: shown('screen-positions')
      ? [...document.querySelectorAll('#positions-list .position-card')].map((c) => ({
        id: c.dataset.position,
        text: c.textContent.replace(/\\s+/g, ' ').trim(),
        chat: c.querySelector('[data-chat]') ? c.querySelector('[data-chat]').dataset.chat : null,
      })) : null,
    journal: shown('positions-journal')
      ? [...document.querySelectorAll('#positions-journal-list li')]
        .map((li) => li.textContent.trim()) : null,
  };
})()"""


# Что показать в замечании «не дождался» — главное о странице.
WAIT_KEYS = ("screens", "last", "market_error", "chart_state", "stale", "error_title",
             "blocked", "lev", "lev_max", "pending", "preview_state", "confirm_card",
             "confirm_status", "confirm_action", "main_button", "positions_state",
             "positions_note", "positions_empty")


class ScenarioError(Exception):
    """Сценарий не может продолжаться (страница не ответила, ошибка JS)."""


@dataclass
class Rule:
    """Подмена сети для запросов, чей адрес содержит `match` (exact — путь адреса равен
    `match`: «/api/open» не ловит «/api/open/preview»), и метод — `method`.

    action: "status" — ответ без сервера (status, body); "patch" — ответ сервера,
    JSON правит `patch`; "fail" — обрыв до сервера (net::ERR_INTERNET_DISCONNECTED);
    "hang" — запрос держится без ответа. times — сколько раз (None — всегда)."""

    match: str
    action: str
    status: int = 200
    body: Any = None
    patch: Callable[[Any], Any] | None = None
    method: str | None = None
    times: int | None = None
    exact: bool = False
    hits: int = 0

    def applies(self, url: str, method: str) -> bool:
        found = urlsplit(url).path == self.match if self.exact else self.match in url
        if not found or (self.method and self.method != method):
            return False
        return self.times is None or self.hits < self.times


@dataclass
class ApiRequest:
    method: str
    path: str                    # без хоста, с query
    body: Any
    at: float

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query)


def _b64(data: Any) -> str:
    return base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()


_DROP_HEADERS = {"content-length", "content-encoding"}


class Page:
    """Страница под управлением DevTools: команды с ожиданием своего ответа, события —
    в Collector, перехват сети по правилам, журнал запросов к /api."""

    def __init__(self, transport: bc.Transport, *, clock: Callable[[], float] = time.monotonic,
                 ) -> None:
        self.transport = transport
        self.clock = clock
        self.collector = bc.Collector()
        self.rules: list[Rule] = []
        self.requests: list[ApiRequest] = []
        self.problems: list[str] = []
        self.notes: list[str] = []
        self.hung: list[str] = []
        self._seq = 0
        self._by_request: dict[str, Rule] = {}
        self.started = clock()
        # Сброс стенда (POST /dev/reset) — задаёт запуск; без него (тесты) шаг пропускается.
        self.reset_stand: Callable[[], Awaitable[None]] | None = None
        # Позиции стенда (POST /dev/positions?preset=…) — так же: задаёт запуск.
        self.seed_stand: Callable[[str], Awaitable[None]] | None = None

    # --- протокол ---------------------------------------------------------------------

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        self._seq += 1
        my = self._seq
        await self.transport.send({"id": my, "method": method, "params": params or {}})
        deadline = self.clock() + timeout
        while my not in self.collector.responses:
            left = deadline - self.clock()
            if left <= 0:
                raise ScenarioError(f"{method}: нет ответа за {timeout} с")
            try:
                msg = await asyncio.wait_for(self.transport.receive(), left)
            except TimeoutError:
                raise ScenarioError(f"{method}: нет ответа за {timeout} с") from None
            await self._handle(msg)
        reply = self.collector.responses.pop(my)
        if "error" in reply:
            raise ScenarioError(f"{method}: {reply['error']}")
        result: dict[str, Any] = reply.get("result", {})
        return result

    async def pump(self, seconds: float) -> None:
        """Ждать, разбирая события (перехват сети работает и во время ожидания)."""
        deadline = self.clock() + seconds
        while (left := deadline - self.clock()) > 0:
            try:
                msg = await asyncio.wait_for(self.transport.receive(), left)
            except TimeoutError:
                return
            await self._handle(msg)

    async def _handle(self, msg: dict[str, Any]) -> None:
        self.collector.feed(msg)
        method = msg.get("method")
        p = msg.get("params", {})
        if method == "Network.requestWillBeSent":
            request = p.get("request", {})
            url = request.get("url", "")
            if "/api/" in url:
                raw = request.get("postData")
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                parts = urlsplit(url)
                path = parts.path + (f"?{parts.query}" if parts.query else "")
                self.requests.append(ApiRequest(request.get("method", "GET"), path, body,
                                                self.clock() - self.started))
        elif method == "Fetch.requestPaused":
            await self._paused(p)

    async def _paused(self, p: dict[str, Any]) -> None:
        rid = p["requestId"]
        url = p.get("request", {}).get("url", "")
        if "responseStatusCode" in p or "responseErrorReason" in p:
            rule = self._by_request.pop(rid, None)
            if rule is None or rule.patch is None or "responseErrorReason" in p:
                await self.call("Fetch.continueRequest", {"requestId": rid})
                return
            got = await self.call("Fetch.getResponseBody", {"requestId": rid})
            raw = got.get("body", "")
            text = base64.b64decode(raw).decode() if got.get("base64Encoded") else raw
            headers = [h for h in p.get("responseHeaders", [])
                       if h.get("name", "").lower() not in _DROP_HEADERS]
            await self.call("Fetch.fulfillRequest", {
                "requestId": rid, "responseCode": p["responseStatusCode"],
                "responseHeaders": headers, "body": _b64(rule.patch(json.loads(text)))})
            return
        method = p.get("request", {}).get("method", "GET")
        rule = next((r for r in self.rules if r.applies(url, method)), None)
        if rule is None:
            await self.call("Fetch.continueRequest", {"requestId": rid})
            return
        rule.hits += 1
        if rule.action == "fail":
            await self.call("Fetch.failRequest",
                            {"requestId": rid, "errorReason": "InternetDisconnected"})
        elif rule.action == "hang":
            self.hung.append(url)
        elif rule.action == "status":
            await self.call("Fetch.fulfillRequest", {
                "requestId": rid, "responseCode": rule.status,
                "responseHeaders": [{"name": "Content-Type", "value": "application/json"}],
                "body": _b64(rule.body if rule.body is not None else {})})
        elif rule.action == "patch":
            self._by_request[rid] = rule
            await self.call("Fetch.continueRequest", {"requestId": rid, "interceptResponse": True})
        else:
            raise ScenarioError(f"неизвестное действие правила: {rule.action}")

    # --- страница --------------------------------------------------------------------

    def intercept(self, rule: Rule) -> Rule:
        self.rules.append(rule)
        return rule

    async def js(self, expression: str) -> Any:
        result = await self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            d = result["exceptionDetails"]
            raise ScenarioError("JS: " + str(d.get("exception", {}).get("description")
                                             or d.get("text")))
        return result.get("result", {}).get("value")

    async def state(self) -> dict[str, Any]:
        value = await self.js(STATE_JS)
        if not isinstance(value, dict):
            raise ScenarioError(f"состояние страницы не прочитано: {value!r}")
        return value

    async def wait_for(self, cond: Callable[[dict[str, Any]], bool], timeout: float,
                       what: str) -> dict[str, Any]:
        """Ждать состояния; не дождались — замечание `what` и последнее состояние."""
        deadline = self.clock() + timeout
        last: dict[str, Any] = {}
        error = ""
        while True:
            try:
                last = await self.state()
                error = ""
            except ScenarioError as exc:
                last, error = {}, str(exc)   # страница ещё грузится
            if last and cond(last):
                return last
            if self.clock() >= deadline:
                seen = error or ", ".join(f"{k}={v!r}" for k, v in last.items()
                                          if k in WAIT_KEYS)
                self.expect(False, f"не дождался за {timeout:g} с: {what} [{seen}]")
                return last
            await self.pump(POLL_STEP)

    def expect(self, ok: bool, text: str) -> bool:
        if not ok:
            self.problems.append(text)
        return ok

    def note(self, text: str) -> None:
        self.notes.append(text)

    def api(self, prefix: str, *, method: str = "GET", since: float = 0.0) -> list[ApiRequest]:
        return [r for r in self.requests
                if r.method == method and r.path.startswith(prefix) and r.at >= since]

    def now(self) -> float:
        return self.clock() - self.started

    async def click(self, selector: str) -> None:
        found = await self.js(f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
                              " if (!e) return false; e.click(); return true; })()")
        if not found:
            raise ScenarioError(f"нет элемента {selector}")

    async def type(self, element_id: str, value: str) -> None:
        await self.js(f"(() => {{ const e = document.getElementById({json.dumps(element_id)});"
                      f" e.value = {json.dumps(value)};"
                      " e.dispatchEvent(new Event('input', {bubbles: true})); })()")

    async def telegram_event(self, name: str, data: Any = None) -> None:
        await self.js(f"Telegram.WebView.receiveEvent({json.dumps(name)}, {json.dumps(data)})")

    async def set_hidden(self, hidden: bool) -> None:
        """Скрытая вкладка: document.hidden + visibilitychange (как при сворачивании)."""
        await self.js("Object.defineProperty(document, 'hidden', {configurable: true, "
                      f"get: () => {json.dumps(hidden)}}});"
                      " document.dispatchEvent(new Event('visibilitychange'))")

    async def viewport(self, width: int, height: int) -> None:
        await self.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": True})

    def issues(self) -> list[str]:
        """Замечания: ожидания + Collector без следов своих правил (их 503, обрывы)."""
        own = [r.match for r in self.rules if r.hits]
        rest = [i for i in self.collector.issues()
                if not (any(m in i for m in own)
                        and i.startswith(("HTTP ", "загрузка не удалась", "log.error")))]
        return self.problems + rest


# --- открытие страницы -------------------------------------------------------------------


@dataclass
class Setup:
    symbol: str | None = SYMBOL
    native: bool = False
    width: int = 390
    height: int = 844
    storage: dict[str, Any] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    wait_screen: str | None = "screen-market"
    preset: str | None = None          # позиции стенда (miniapp_dev.PRESETS)


def storage_script(values: dict[str, Any]) -> str:
    sets = " ".join(f"localStorage.setItem({json.dumps(STORAGE + k)}, {json.dumps(json.dumps(v))});"
                    for k, v in values.items())
    return f"try {{ {sets} }} catch (e) {{}}"


async def open_page(page: Page, url: str, setup: Setup) -> dict[str, Any]:
    # Чистый стенд: позиции, лимиты и сделки прошлых сценариев не дают BLOCK.
    if page.reset_stand is not None:
        await page.reset_stand()
    if setup.preset is not None and page.seed_stand is not None:
        await page.seed_stand(setup.preset)
    for method in ("Runtime.enable", "Log.enable", "Network.enable", "Page.enable"):
        await page.call(method)
    await page.call("Network.setCacheDisabled", {"cacheDisabled": True})
    await page.call("Page.addScriptToEvaluateOnNewDocument", {"source": bc.CSP_LISTENER})
    values = dict(setup.storage)
    if setup.symbol:
        values["symbol"] = bc.require_symbol(setup.symbol)
    if values:
        await page.call("Page.addScriptToEvaluateOnNewDocument",
                        {"source": storage_script(values)})
    await page.viewport(setup.width, setup.height)
    for rule in setup.rules:
        page.intercept(rule)
    await page.call("Fetch.enable", {"patterns": [
        {"urlPattern": "*/api/*", "requestStage": "Request"},
        {"urlPattern": "*telegram.org*", "requestStage": "Request"},
    ]})
    await page.call("Page.navigate", {"url": url + (NATIVE_HASH if setup.native else "")})
    if setup.wait_screen is None:
        return {}
    want = setup.wait_screen
    ready = await page.wait_for(
        lambda s: "screen-sdk" in s["screens"]
        or (want in s["screens"] and (want != "screen-market" or not setup.symbol
                                      or (s["last"] not in (None, "—")
                                          and s["market_charts"] == 1))),
        bc.START_TIMEOUT, f"экран {want}")
    if "screen-sdk" in ready.get("screens", []):
        # Сеть до telegram.org, не код: дальше сценарий смысла не имеет.
        raise ScenarioError("SDK telegram.org не загрузился (сеть) — повтори сценарий")
    await page.pump(0.5)
    return ready


# --- сценарии ----------------------------------------------------------------------------

ScenarioFn = Callable[[Page, str], Awaitable[None]]


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    fn: ScenarioFn


SCENARIOS: dict[str, Scenario] = {}


def scenario(name: str, title: str) -> Callable[[ScenarioFn], ScenarioFn]:
    def register(fn: ScenarioFn) -> ScenarioFn:
        SCENARIOS[name] = Scenario(name, title, fn)
        return fn
    return register


def _market(path: str) -> bool:
    return path.startswith("/api/market/") and "/candles" not in path


# 2.3 — экран рынка


@scenario("search", "2.3: без монеты — поиск, «xrp» → XRP-USDT → рынок с одним графиком")
async def search(page: Page, url: str) -> None:
    await open_page(page, url, Setup(symbol=None))
    s = await page.wait_for(lambda s: s["search"], 5, "поиск при первом запуске")
    page.expect(s.get("market_charts") == 0, "график до выбора монеты")
    await page.type("market-search-input", "xrp")
    await page.wait_for(lambda s: SYMBOL in s["search_items"], 5, "XRP-USDT в выдаче")
    page.expect(len(page.api("/api/symbols")) == 1,
                f"поиск: запросов {len(page.api('/api/symbols'))}, ждали 1 (debounce)")
    await page.click(f'[data-symbol="{SYMBOL}"]')
    await page.wait_for(lambda s: s["market_charts"] == 1 and s["last"] not in (None, "—")
                        and s["symbol"] == SYMBOL, 8, "рынок XRP-USDT с графиком")


@scenario("poll", "2.3: опрос цены раз в 3 с цепочкой, свечи — не на каждом опросе")
async def poll(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    start = page.now()
    await page.pump(10)
    market = [r for r in page.requests if _market(r.path) and r.at >= start]
    candles = page.api("/api/market/XRP-USDT/candles", since=start)
    page.note(f"за 10 с: /market {len(market)}, /candles {len(candles)}")
    page.expect(3 <= len(market) <= 4, f"/api/market за 10 с: {len(market)}, ждали 3–4")
    page.expect(len(candles) <= 1, f"/candles за 10 с: {len(candles)}, ждали ≤ 1")


@scenario("timeframe", "2.3: таймфрейм 1ч и тип «Линия» — запрос свечей 1h, выбор помнится")
async def timeframe(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    since = page.now()
    await page.click('#market-intervals [data-interval="1h"]')
    s = await page.wait_for(lambda s: s["interval_label"] == "1ч" and s["market_charts"] == 1,
                            6, "подпись «1ч»")
    candles = page.api("/api/market/XRP-USDT/candles", since=since)
    page.expect(any(r.query.get("interval") == ["1h"] for r in candles), "нет /candles?interval=1h")
    await page.click('#market-types [data-type="line"]')
    stored = await page.js(f"localStorage.getItem('{STORAGE}chartType')")
    page.expect(stored == '"line"', f"тип графика не запомнен: {stored!r}")
    interval = await page.js(f"localStorage.getItem('{STORAGE}interval')")
    page.expect(interval == '"1h"', f"таймфрейм не запомнен: {interval!r}")
    page.expect(s.get("charts") == 1, f"графиков {s.get('charts')}, ждали 1")


@scenario("theme", "2.3: theme_changed — фон графика перекрашен в bg_color Telegram")
async def theme(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True))
    pixel = ("(() => { const c = document.querySelector('#market-chart canvas');"
             " const d = c.getContext('2d').getImageData(3, 3, 1, 1).data;"
             " return [d[0], d[1], d[2]]; })()")
    await page.telegram_event("theme_changed", {"theme_params": {
        "bg_color": "#0e1621", "text_color": "#f5f5f5", "hint_color": "#708499"}})
    await page.pump(1)
    got = await page.js(pixel)
    page.expect(got == [14, 22, 33], f"фон графика {got}, ждали [14, 22, 33] (#0e1621)")


@scenario("widths", "2.3/2.4: 320 и 390 px без прокрутки вбок, поворот — график по ширине")
async def widths(page: Page, url: str) -> None:
    await open_page(page, url, Setup(width=320))
    for width in (320, 390):
        await page.viewport(width, 844)
        await page.pump(1)
        s = await page.state()
        page.expect(s["scroll_x"] == 0, f"рынок {width} px: прокрутка {s['scroll_x']} px")
    for w, h in ((844, 390), (390, 844)):
        await page.viewport(w, h)
        await page.pump(1.5)
        s = await page.state()
        page.expect(abs((s["chart_box_width"] or 0) - (s["chart_canvas_width"] or -9)) <= 2,
                    f"{w}×{h}: график {s['chart_canvas_width']} px при ширине "
                    f"{s['chart_box_width']} px")
    await page.click("#fallback-main")
    await page.wait_for(lambda s: "screen-trade" in s["screens"] and s["form_shown"], 8, "форма")
    for width in (320, 390):
        await page.viewport(width, 844)
        await page.pump(1)
        s = await page.state()
        page.expect(s["scroll_x"] == 0, f"форма {width} px: прокрутка {s['scroll_x']} px")


@scenario("hidden", "2.3: скрытая вкладка — опроса нет; снова видна — опрос идёт")
async def hidden(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    await page.set_hidden(True)
    await page.pump(0.5)
    start = page.now()
    await page.pump(7)
    quiet = [r for r in page.requests if r.at >= start]
    page.expect(not quiet, f"скрытая вкладка: запросов {len(quiet)}, ждали 0")
    await page.set_hidden(False)
    start = page.now()
    await page.pump(4)
    page.expect(any(_market(r.path) for r in page.requests if r.at >= start),
                "вкладка снова видна — опрос не возобновился")


@scenario("leave_return", "2.3/2.4: рынок → форма → «Назад» ×5 — график снят и создан заново, один")
async def leave_return(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True))
    for i in range(5):
        await page.telegram_event("main_button_pressed")
        s = await page.wait_for(lambda s: "screen-trade" in s["screens"] and s["trade_charts"] == 1,
                                8, f"форма, круг {i + 1}")
        page.expect(s.get("market_charts") == 0, f"круг {i + 1}: график рынка не снят")
        await page.telegram_event("back_button_pressed")
        s = await page.wait_for(lambda s: "screen-market" in s["screens"]
                                and s["market_charts"] == 1 and s["last"] not in (None, "—"),
                                8, f"рынок, круг {i + 1}")
        page.expect(s.get("charts") == 1, f"круг {i + 1}: графиков {s.get('charts')}, ждали 1")
    start = page.now()
    await page.pump(7)
    market = [r for r in page.requests if _market(r.path) and r.at >= start]
    page.expect(len(market) <= 3, f"после 5 кругов /api/market за 7 с: {len(market)} — "
                                  "опрос задвоился")


@scenario("empty_candles", "2.3: биржа не вернула свечей — «Свечей нет»")
async def empty_candles(page: Page, url: str) -> None:
    await open_page(page, url, Setup(rules=[Rule("/candles", "patch",
                                                 patch=lambda d: {**d, "candles": []})],
                                     wait_screen=None))
    await page.wait_for(lambda s: (s["chart_state"] or "").startswith("Свечей нет"), 10,
                        "«Свечей нет»")


EXCHANGE_DOWN = {"code": "EXCHANGE_UNAVAILABLE", "message": "Биржа не ответила (Test) — повтори."}


@scenario("load_503", "2.3: 503 при загрузке — текст сервера и «Повторить», повтор открывает рынок")
async def load_503(page: Page, url: str) -> None:
    await open_page(page, url, Setup(rules=[Rule(f"/api/market/{SYMBOL}?", "status", status=503,
                                                 body=EXCHANGE_DOWN, times=1)],
                                     wait_screen=None))
    await page.wait_for(lambda s: s["market_error"] == EXCHANGE_DOWN["message"], 10,
                        "ошибка загрузки с текстом сервера")
    await page.click("#market-retry")
    await page.wait_for(lambda s: s["market_charts"] == 1 and s["last"] not in (None, "—")
                        and s["market_error"] is None, 8, "рынок после «Повторить»")


@scenario("poll_503", "2.3: 503 в опросе — цифры остаются, пометка «устаревшие», потом снимается")
async def poll_503(page: Page, url: str) -> None:
    ready = await open_page(page, url, Setup())
    page.intercept(Rule(f"/api/market/{SYMBOL}?", "status", status=503, body=EXCHANGE_DOWN,
                        times=2))
    s = await page.wait_for(lambda s: s["stale"] is not None, 8, "пометка устаревших цен")
    page.expect(s.get("last") == ready.get("last"), "цена пропала при ошибке опроса")
    page.expect(EXCHANGE_DOWN["message"] in (s.get("stale") or ""), "в пометке нет текста сервера")
    await page.wait_for(lambda s: s["stale"] is None, 30, "пометка снята после восстановления")


@scenario("expired_401", "2.3: 401 в опросе — «Сессия истекла», опрос встал")
async def expired_401(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    page.intercept(Rule("/api/market/", "status", status=401,
                        body={"code": "UNAUTHORIZED", "message": "Сессия устарела"}))
    await page.wait_for(lambda s: s["error_title"] == "Сессия истекла", 8, "«Сессия истекла»")
    start = page.now()
    await page.pump(7)
    after = [r for r in page.requests if r.at >= start]
    page.expect(not after, f"после 401 запросов {len(after)}, ждали 0")


# 2.3а — свечи 1м


@scenario("candles_1m", "2.3а: ТФ 1м, 65 с — /candles только на смене минуты и раз в 60 с")
async def candles_1m(page: Page, url: str) -> None:
    await open_page(page, url, Setup(storage={"interval": "1m"}))
    start = page.now()
    await page.pump(65)
    candles = page.api("/api/market/XRP-USDT/candles", since=start)
    times = [round(r.at - start, 1) for r in candles]
    page.note(f"/candles за 65 с: {times}")
    page.expect(len(candles) <= 3, f"/candles за 65 с: {len(candles)}, ждали ≤ 3")
    gaps = [b - a for a, b in itertools.pairwise(times)]
    page.expect(all(g >= 9 for g in gaps), f"/candles чаще 10 с: {times}")


# 2а — SDK telegram.org


SDK_TEXT = "Не загрузилось. Проверь интернет и открой заново из бота."


@scenario("sdk_block", "2а: SDK заблокирован — сразу «Не загрузилось», запросов к API нет")
async def sdk_block(page: Page, url: str) -> None:
    await open_page(page, url, Setup(rules=[Rule("telegram.org", "fail")], wait_screen=None))
    s = await page.wait_for(lambda s: "screen-sdk" in s["screens"], 4, "экран «Не загрузилось»")
    page.expect(s.get("sdk_text", "").startswith(SDK_TEXT) if s else False,
                f"текст экрана: {s.get('sdk_text')!r}")
    await page.pump(3)
    page.expect(not page.requests, f"запросов к API: {len(page.requests)}, ждали 0")


@scenario("sdk_hang", "2а: SDK завис — «Загрузка…», через 10 с «Не загрузилось», API нет")
async def sdk_hang(page: Page, url: str) -> None:
    await open_page(page, url, Setup(rules=[Rule("telegram.org", "hang")], wait_screen=None))
    await page.pump(5)
    s = await page.state()
    page.expect(s["screens"] == ["screen-loading"], f"на 5 с экраны {s['screens']}")
    await page.wait_for(lambda s: "screen-sdk" in s["screens"], 8, "«Не загрузилось» после 10 с")
    page.expect(not page.requests, f"запросов к API: {len(page.requests)}, ждали 0")


# 2.4 — форма сделки


def _previews(page: Page, since: float = 0.0) -> list[ApiRequest]:
    return page.api("/api/open/preview", method="POST", since=since)


def _patch_me(max_leverage: int | None) -> Callable[[Any], Any]:
    def patch(data: Any) -> Any:
        data["plan"]["max_leverage"] = max_leverage
        return data
    return patch


def _no_leverage(error: str) -> Callable[[Any], Any]:
    def patch(data: Any) -> Any:
        data["leverage"] = None
        data["contract"]["max_leverage"] = None
        data["leverage_error"] = error
        return data
    return patch


async def _expected_cap(page: Page) -> int:
    """Потолок по ответам стенда (с правилами сценария): min(максимум лонга, план)."""
    me = await page.js("fetch('/api/me').then((r) => r.json())")
    market = await page.js(f"fetch('/api/market/{SYMBOL}?interval=5m').then((r) => r.json())")
    side_max = int(market["leverage"]["max_long"])
    plan = me["plan"]["max_leverage"]
    return min(side_max, plan) if plan else side_max


def _button(s: dict[str, Any], native: bool) -> str | None:
    return s.get("main_button") if native else s.get("fallback")


async def _to_form(page: Page, native: bool) -> dict[str, Any]:
    if native:
        await page.telegram_event("main_button_pressed")
    else:
        await page.click("#fallback-main")
    return await page.wait_for(lambda s: "screen-trade" in s["screens"]
                               and (s["blocked"] is not None or s["lev_max"] != "1"),
                               8, "форма сделки")


async def _settled(page: Page, what: str) -> dict[str, Any]:
    await page.pump(0.5)
    return await page.wait_for(lambda s: not s["pending"], 5, what)


async def form_flow(page: Page, native: bool) -> None:
    cap = await _expected_cap(page)
    s = await _to_form(page, native)
    page.note(f"потолок {s.get('lev_max')}×, ждали {cap}×")
    page.expect(s.get("lev_max") == str(cap), f"потолок плеча {s.get('lev_max')}, ждали {cap}")
    page.expect("Укажи стоп" in (s.get("preview_state") or ""), "без стопа нет «Укажи стоп»")
    page.expect(not _previews(page), "превью ушло без стопа")
    page.expect(_button(s, native) is None, "кнопка «Открыть» без стопа")

    await page.click('#trade-stop-chips [data-percent="2"]')
    s = await _settled(page, "превью стопа 2%")
    body = _previews(page)[-1].body if _previews(page) else {}
    page.expect(body.get("stop") == {"kind": "percent", "value": "2"},
                f"стоп ушёл не процентом: {body.get('stop')}")
    page.expect((_button(s, native) or "").startswith("Открыть ЛОНГ"),
                f"кнопка: {_button(s, native)!r}")
    page.expect(int(s.get("lev") or 0) <= cap, f"плечо {s.get('lev')} выше потолка {cap}")
    page.expect(s.get("take_2r_disabled") is False, "чип 2R неактивен при стопе")

    await page.click("#trade-take-2r")
    s = await _settled(page, "превью 2R")
    page.expect("RR2.00" in (s.get("result") or "").replace(" ", ""), f"2R: {s.get('result')!r}")
    page.expect(_previews(page)[-1].body.get("take") == {"kind": "price", "value": s.get("take")},
                "2R: тейк ушёл не ценой сервера")

    since = page.now()
    await page.js("(async () => { const i = document.getElementById('trade-take');"
                  " for (const v of ['1', '1.', '1.6', '1.60', '1.605']) { i.value = v;"
                  " i.dispatchEvent(new Event('input'));"
                  " await new Promise((r) => setTimeout(r, 40)); }"
                  " })()")
    await _settled(page, "превью тейка")
    sent = _previews(page, since)
    page.expect(len(sent) == 1, f"5 изменений за 200 мс → превью {len(sent)}, ждали 1")

    await page.js("(() => { const r = document.getElementById('trade-leverage'); r.value = '999';"
                  " r.dispatchEvent(new Event('input')); })()")
    s = await _settled(page, "превью плеча 999")
    page.expect(s.get("lev") == str(cap) and s.get("lev_max") == str(cap),
                f"ползунок 999: {s.get('lev')}/{s.get('lev_max')}, ждали {cap}")
    page.expect(_previews(page)[-1].body.get("leverage") == cap,
                f"в теле плечо {_previews(page)[-1].body.get('leverage')}, ждали {cap}")

    await page.type("trade-risk", "50")
    s = await _settled(page, "превью риска 50%")
    page.expect("🚫" in (s.get("result") or ""), "риск 50% без 🚫")
    page.expect(_button(s, native) is None, "кнопка «Открыть» при BLOCK")

    since = page.now()
    await page.type("trade-risk", "abc")
    await page.pump(1)
    s = await page.state()
    page.expect(s["risk_invalid"] is True, "«abc» не подсвечено")
    page.expect(not _previews(page, since), "превью ушло с «abc»")

    await page.type("trade-risk", "1,5")
    await _settled(page, "превью риска 1,5")
    page.expect(_previews(page)[-1].body.get("risk") == {"kind": "percent", "value": "1.5"},
                f"риск «1,5» ушёл как {_previews(page)[-1].body.get('risk')}")

    await page.click('#trade-take-chips [data-percent=""]')
    s = await _settled(page, "без тейка")
    page.expect((_button(s, native) or "").startswith("Открыть ЛОНГ"),
                f"без тейка кнопка: {_button(s, native)!r}")
    if native:
        await page.telegram_event("main_button_pressed")
    else:
        await page.click("#fallback-main")
    await page.wait_for(lambda s: "screen-confirm" in s["screens"], 4, "заглушка подтверждения")


async def blocked_flow(page: Page, reason: str) -> None:
    s = await _to_form(page, False)
    text = s.get("blocked") or ""
    page.expect(text.startswith("Подключи ключи биржи"), f"нет «Подключи ключи биржи»: {text!r}")
    page.expect(reason in text, f"нет причины «{reason}»: {text!r}")
    page.expect(not s.get("form_shown"), "форма видна без потолка плеча")
    start = page.now()
    await page.pump(7)
    s = await page.state()
    page.expect(not _previews(page, start), "превью без ключа")
    page.expect(s["fallback"] is None, "кнопка «Открыть» без ключа")


@scenario("form", "2.4: форма — потолок min(сторона, план), 2R, debounce, BLOCK, ввод, «Открыть»")
async def form(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    await form_flow(page, native=False)


@scenario("form_native", "2.4: то же с нативным MainButton (Telegram 8.0)")
async def form_native(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True))
    await form_flow(page, native=True)


@scenario("form_plan10", "2.4: план 10× — потолок 10×")
async def form_plan10(page: Page, url: str) -> None:
    await open_page(page, url, Setup(rules=[Rule("/api/me", "patch", patch=_patch_me(10))]))
    await form_flow(page, native=False)


@scenario("form_planfree", "2.4: план без лимита плеча — потолок = максимум стороны")
async def form_planfree(page: Page, url: str) -> None:
    await open_page(page, url, Setup(rules=[Rule("/api/me", "patch", patch=_patch_me(None))]))
    cap = await _expected_cap(page)
    page.expect(cap == 125, f"ожидаемый потолок {cap}, ждали максимум стороны стенда 125")
    s = await _to_form(page, False)
    page.expect(s.get("lev_max") == str(cap), f"потолок {s.get('lev_max')}, ждали {cap} (сторона)")
    await page.js("(() => { const r = document.getElementById('trade-leverage'); r.value = '999';"
                  " r.dispatchEvent(new Event('input')); })()")
    s = await page.wait_for(lambda s: s["lev"] == str(cap), 3, f"ползунок 999 → {cap}")


NO_KEY = "Ключ BingX не подключён — подключи в настройках бота."
LEV_FAIL = "Плечо не прочитано (ExchangeError)."


@scenario("form_nokey", "2.4: нет ключа — «Подключи ключи биржи», превью и кнопки нет")
async def form_nokey(page: Page, url: str) -> None:
    await open_page(page, url, Setup(rules=[Rule(f"/api/market/{SYMBOL}?", "patch",
                                                 patch=_no_leverage(NO_KEY))]))
    await blocked_flow(page, NO_KEY)


@scenario("form_leverr", "2.4: максимум плеча не прочитан — то же, с причиной сбоя")
async def form_leverr(page: Page, url: str) -> None:
    await open_page(page, url, Setup(rules=[Rule(f"/api/market/{SYMBOL}?", "patch",
                                                 patch=_no_leverage(LEV_FAIL))]))
    await blocked_flow(page, LEV_FAIL)


# 2.5 — подтверждение и отправка (на стенде — фейковая биржа, бота нет: в чат не уходит)

# showConfirm Telegram в headless не ответить — подменяем: вопрос в __asked, ответ «ОК».
FAKE_SHOW_CONFIRM = ("window.__asked = []; Telegram.WebApp.showConfirm = (m, cb) => {"
                     " window.__asked.push(m); setTimeout(() => cb(true), 50); }")


def _confirms(page: Page, since: float = 0.0) -> list[ApiRequest]:
    return [r for r in page.api("/api/open/", method="POST", since=since)
            if r.path.endswith("/confirm")]


def _cards(page: Page, since: float = 0.0) -> list[ApiRequest]:
    return [r for r in page.api("/api/open", method="POST", since=since) if r.path == "/api/open"]


DRY_RUN_TEXT = ("стенд в сухом прогоне — для сценариев 2.5 запусти его с "
                "EXEC_OPEN_DRY_RUN=false (биржа стенда фейковая)")


async def _ready_form(page: Page, native: bool, *, stop: str = "2",
                      take: str | None = None, entry: str | None = None) -> dict[str, Any]:
    me = await page.js("fetch('/api/me').then((r) => r.json())")
    if me["flags"]["dry_run"]:
        raise ScenarioError(DRY_RUN_TEXT)
    await _to_form(page, native)
    if entry == "LIMIT":
        await page.click('#trade-entry [data-entry="LIMIT"]')
        await page.click('#trade-stop-kind [data-kind="percent"]')
        await page.click('#trade-take-kind [data-kind="percent"]')
    await page.click(f'#trade-stop-chips [data-percent="{stop}"]')
    if take is not None:
        await page.click('#trade-take-kind [data-kind="percent"]')
        await page.type("trade-take", take)
    return await _settled(page, "превью перед «Открыть»")


async def _to_confirm(page: Page, native: bool) -> dict[str, Any]:
    if native:
        await page.telegram_event("main_button_pressed")
    else:
        await page.click("#fallback-main")
    return await page.wait_for(lambda s: "screen-confirm" in s["screens"]
                               and s["confirm_card"] is not None
                               and not s["confirm_card"].startswith("Готовлю"),
                               10, "карточка подтверждения")


async def _press(page: Page, native: bool) -> None:
    """Главная кнопка экрана подтверждения; без showConfirm (6.0) — два нажатия."""
    if native:
        await page.telegram_event("main_button_pressed")
    else:
        await page.click("#confirm-action")
        await page.pump(0.2)
        await page.click("#confirm-action")


def _done(s: dict[str, Any]) -> bool:
    return "Сделка #" in (s.get("confirm_card") or "")


async def _open_and_confirm(page: Page, native: bool) -> dict[str, Any]:
    s = await _to_confirm(page, native)
    page.expect("Карточка действует ещё" in (s.get("confirm_timer") or ""),
                f"нет отсчёта: {s.get('confirm_timer')!r}")
    label = s.get("main_button") if native else s.get("confirm_action")
    page.expect(label in ("Подтвердить", "Открыть всё равно"), f"кнопка карточки: {label!r}")
    await _press(page, native)
    return await page.wait_for(_done, 25, "итог «Сделка #N»")


@scenario("confirm_market", "2.5: маркет — карточка, отсчёт, два нажатия (6.0) → «Сделка #N»")
async def confirm_market(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    await _ready_form(page, False)
    s = await _open_and_confirm(page, False)
    page.expect(len(_cards(page)) == 1, f"POST /api/open: {len(_cards(page))}, ждали 1")
    page.expect(len(_confirms(page)) == 1, f"/confirm: {len(_confirms(page))}, ждали 1")
    page.expect("Стоп ✓" in (s.get("confirm_card") or ""), "в итоге нет «Стоп ✓»")
    page.expect(s.get("confirm_action") == "Закрыть" and s.get("confirm_back") == "К рынку",
                f"кнопки итога: {s.get('confirm_action')!r} / {s.get('confirm_back')!r}")
    await page.click("#confirm-back")
    await page.wait_for(lambda s: "screen-market" in s["screens"] and s["market_charts"] == 1,
                        8, "«К рынку»")


@scenario("confirm_native", "2.5: нативный MainButton и showConfirm (сторона, объём, риск $)")
async def confirm_native(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True))
    await page.js(FAKE_SHOW_CONFIRM)
    await _ready_form(page, True)
    await _open_and_confirm(page, True)
    asked = await page.js("window.__asked")
    page.expect(len(asked) == 1 and asked[0].startswith("Открыть ЛОНГ ") and " риск " in asked[0],
                f"showConfirm: {asked!r}")
    page.expect(not any("Реальный счёт" in a for a in asked), "DEMO, а в вопросе «Реальный счёт»")
    page.expect(len(_confirms(page)) == 1, f"/confirm: {len(_confirms(page))}, ждали 1")


def _live(data: Any) -> Any:
    data["account_mode"] = "LIVE"
    return data


@scenario("confirm_live", "2.5: LIVE — «⚠️ Реальный счёт» на подтверждении и в showConfirm")
async def confirm_live(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True, rules=[Rule("/api/me", "patch", patch=_live)]))
    await page.js(FAKE_SHOW_CONFIRM)
    await _ready_form(page, True)
    s = await _to_confirm(page, True)
    page.expect(s.get("confirm_live") is True, "на подтверждении нет «⚠️ Реальный счёт»")
    await page.telegram_event("main_button_pressed")
    await page.pump(0.5)
    asked = await page.js("window.__asked")
    page.expect(bool(asked) and asked[0].startswith("⚠️ Реальный счёт\nОткрыть ЛОНГ"),
                f"showConfirm на LIVE: {asked!r}")


@scenario("confirm_retry", "2.5: обрыв до сервера на /confirm → CARD → «Не дошло» → повтор")
async def confirm_retry(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    page.intercept(Rule("/confirm", "fail", method="POST", times=1))
    await _ready_form(page, False)
    await _to_confirm(page, False)
    await _press(page, False)
    s = await page.wait_for(lambda s: "Не дошло" in (s["confirm_status"] or ""), 10,
                            "«Не дошло, нажми ещё раз»")
    page.expect(s.get("confirm_action") == "Подтвердить", f"кнопка: {s.get('confirm_action')!r}")
    ids = {r.path for r in _confirms(page)}
    await _press(page, False)
    s = await page.wait_for(_done, 25, "итог после повтора")
    sent = _confirms(page)
    page.expect(len(sent) == 2 and {r.path for r in sent} == ids,
                f"/confirm: {[r.path for r in sent]} — ждали 2 по одному id")
    page.expect(len(_cards(page)) == 1, "повтор создал новую карточку")
    gets = [r for r in page.requests if r.method == "GET" and r.path.startswith("/api/open/")]
    page.expect(bool(gets), "после обрыва не было опроса GET /api/open/{id}")


@scenario("confirm_warn", "2.5: WARN — «Открыть всё равно» на форме и на карточке → сделка")
async def confirm_warn(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    s = await _ready_form(page, False, take="0.5")
    page.expect(s.get("fallback") == "Открыть всё равно", f"форма: {s.get('fallback')!r}")
    s = await _to_confirm(page, False)
    page.expect(s.get("confirm_action") == "Открыть всё равно",
                f"карточка: {s.get('confirm_action')!r}")
    await _press(page, False)
    await page.wait_for(_done, 25, "итог после «Открыть всё равно»")
    sent = _confirms(page)
    page.expect(bool(sent) and sent[-1].body == {"accept_warnings": True},
                f"тело /confirm: {sent[-1].body if sent else None}")


@scenario("confirm_limit", "2.5: лимит — «⏳ Лимит выставлен», «Отменить лимит» → отменён")
async def confirm_limit(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    await _to_form(page, False)
    await page.click('#trade-entry [data-entry="LIMIT"]')
    await page.type("trade-limit", "1.4000")
    await page.click('#trade-stop-chips [data-percent="2"]')
    await _settled(page, "превью лимита")
    await _to_confirm(page, False)
    await _press(page, False)
    s = await page.wait_for(lambda s: "Лимит выставлен" in (s["confirm_card"] or ""), 20,
                            "«⏳ Лимит выставлен»")
    page.expect("истекает" in (s.get("confirm_card") or ""), "нет срока лимита")
    page.expect(s.get("confirm_action") == "Отменить лимит", f"кнопка: {s.get('confirm_action')!r}")
    await _press(page, False)
    s = await page.wait_for(lambda s: s["confirm_action"] == "Закрыть", 15, "итог отмены")
    page.expect(any(r.path.endswith("/cancel") for r in page.api("/api/open/", method="POST")),
                "не было POST /cancel")
    page.note(f"итог отмены: {(s.get('confirm_card') or '')[:80]!r}")


def _expires_soon(data: Any) -> Any:
    soon = time.time() + 3
    data["expires_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(soon)) + "Z"
    return data


@scenario("confirm_expired", "2.5: отсчёт дошёл до нуля — «Пересчитать» → новая карточка")
async def confirm_expired(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    page.intercept(Rule("/api/open", "patch", method="POST", patch=_expires_soon, times=1,
                        exact=True))
    await _ready_form(page, False)
    await _to_confirm(page, False)
    s = await page.wait_for(lambda s: s["confirm_action"] == "Пересчитать", 8, "«Пересчитать»")
    page.expect("устарела" in (s.get("confirm_status") or ""), "нет «Карточка устарела»")
    await page.click("#confirm-action")
    s = await page.wait_for(lambda s: s["confirm_action"] == "Подтвердить", 10, "новая карточка")
    page.expect(len(_cards(page)) == 2, f"POST /api/open: {len(_cards(page))}, ждали 2")
    page.expect(not _confirms(page), "истёкшую карточку подтвердили")


def _blocked_card(data: Any) -> Any:
    data["card"]["can_open"] = False
    return data


@scenario("confirm_block", "2.5: BLOCK на свежих данных — кнопки нет, «Назад к форме»")
async def confirm_block(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True))
    page.intercept(Rule("/api/open", "patch", method="POST", patch=_blocked_card, exact=True))
    await _ready_form(page, True)
    s = await _to_confirm(page, True)
    page.expect(s.get("main_button") is None, f"кнопка при BLOCK: {s.get('main_button')!r}")
    page.expect(s.get("confirm_back") == "Назад к форме",
                f"вторая кнопка: {s.get('confirm_back')!r}")


@scenario("confirm_back", "2.5: «Назад к форме» — поля те же (только в сессии)")
async def confirm_back(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    await _ready_form(page, False, stop="3")
    await _to_confirm(page, False)
    await page.click("#confirm-back")
    s = await page.wait_for(lambda s: "screen-trade" in s["screens"] and s["form_shown"], 8,
                            "форма после «Назад»")
    page.expect(s.get("stop") == "3", f"стоп после «Назад»: {s.get('stop')!r}, ждали 3")
    page.expect(not _confirms(page), "«Назад» отправил /confirm")
    stored = await page.js("Object.keys(localStorage).filter((k) => /stop|risk|take/.test(k))")
    page.expect(stored == [], f"поля формы в localStorage: {stored}")


# П.1 — вкладка «Позиции» и меню «…»

FAKE_POPUP = ("window.__popups = []; window.__choice = 'positions';"
              " Telegram.WebApp.showPopup = (p, cb) => {"
              " window.__popups.push(p); setTimeout(() => cb(window.__choice), 50); }")
FAKE_TG_LINK = ("window.__links = []; Telegram.WebApp.openTelegramLink = (u) =>"
                " window.__links.push(u);")
POSITIONS_NO_KEY = {"code": "NO_KEY",
                    "message": "🔑 Ключи биржи не подключены — показан только журнал."}


def _positions_requests(page: Page, since: float = 0.0) -> list[ApiRequest]:
    return page.api("/api/positions", since=since)


def _loaded(s: dict[str, Any]) -> bool:
    return ("screen-positions" in s["screens"] and s["positions_state"] is None
            and s["positions"] is not None)


async def _to_positions(page: Page) -> dict[str, Any]:
    """Своя ссылка «💼 Позиции» (браузер 6.0 — SettingsButton нет)."""
    await page.click("#account-positions")
    return await page.wait_for(_loaded, 10, "вкладка «Позиции»")


def _card(s: dict[str, Any], side: str) -> dict[str, Any]:
    found = [c for c in s.get("positions") or [] if c["id"] == f"{SYMBOL}:{side}"]
    return found[0] if found else {"text": "", "chat": None}


@scenario("pos_empty", "П.1: позиций нет — «На DEMO открытых позиций нет» и «Открыть сделку»")
async def pos_empty(page: Page, url: str) -> None:
    s = await open_page(page, url, Setup())
    page.expect(s.get("menu_link") is True and s.get("settings_button") is not True,
                "6.0: нет своей ссылки «💼 Позиции»")
    s = await _to_positions(page)
    page.expect(s.get("positions_empty") == "На DEMO открытых позиций нет.",
                f"пустая вкладка: {s.get('positions_empty')!r}")
    page.expect(s.get("journal") is None, "журнал пуст, а блок показан")
    page.expect(s.get("main_button") is None and s.get("fallback") is None,
                "на вкладке видны кнопки «Лонг»/«Шорт»")
    await page.click("#positions-to-market")
    await page.wait_for(lambda s: "screen-market" in s["screens"] and s["market_charts"] == 1,
                        8, "рынок по «Открыть сделку»")


@scenario("pos_list", "П.1: хедж — лестница стопов, без стопа, журнал, «только в журнале»")
async def pos_list(page: Page, url: str) -> None:
    await open_page(page, url, Setup(preset="hedge"))
    s = await _to_positions(page)
    long, short = _card(s, "LONG"), _card(s, "SHORT")
    page.expect(len(s.get("positions") or []) == 2, f"карточек {len(s.get('positions') or [])}")
    page.expect("2 ордера: 1.4402 (на всю позицию); 1.4100 (на всю позицию)" in long["text"],
                f"лестница стопов: {long['text']!r}")
    page.expect("Тейк1.5631 (100)" in long["text"], f"тейк на часть: {long['text']!r}")
    page.expect("сделка #" in long["text"], "LONG не связан с DEMO-сделкой")
    page.expect("Стопнет" in short["text"] and "не в журнале" in short["text"],
                f"SHORT: {short['text']!r}")
    journal = s.get("journal") or []
    page.expect(len(journal) == 2 and any("без счёта" in j for j in journal)
                and any("реальный счёт" in j for j in journal), f"журнал: {journal}")
    # На стенде бота нет (bot_username=None) — кнопки в чат нет, есть подсказка.
    page.expect(long["chat"] is None and "в чате бота" in long["text"],
                "без имени бота — кнопка в чат или нет подсказки")


@scenario("pos_unlinked", "П.1: в журнале только ручная (NULL) и LIVE — позиция «не в журнале»")
async def pos_unlinked(page: Page, url: str) -> None:
    await open_page(page, url, Setup(preset="unlinked"))
    s = await _to_positions(page)
    long = _card(s, "LONG")
    page.expect("не в журнале" in long["text"] and "сделка #" not in long["text"],
                f"связалась чужая сделка: {long['text']!r}")
    journal = s.get("journal") or []
    page.expect(len(journal) == 2 and all("XRP-USDT LONG" in j for j in journal),
                f"журнал: {journal}")


def _with_bot(data: Any) -> Any:
    data["bot_username"] = "testbot"
    return data


@scenario("pos_chat", "П.1: «⚙️ Действия в чате» — openTelegramLink t.me/<бот>?start=pos-…")
async def pos_chat(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True, preset="hedge",
                                     rules=[Rule("/api/me", "patch", patch=_with_bot)]))
    await page.js(FAKE_TG_LINK)
    await page.js(FAKE_POPUP)
    await page.telegram_event("settings_button_pressed")
    s = await page.wait_for(_loaded, 10, "вкладка из меню «…»")
    page.expect(_card(s, "LONG")["chat"] == "https://t.me/testbot?start=pos-XRP-USDT-L",
                f"ссылка LONG: {_card(s, 'LONG')['chat']!r}")
    await page.click('[data-position="XRP-USDT:SHORT"] [data-chat]')
    await page.pump(0.3)
    links = await page.js("window.__links")
    page.expect(links == ["https://t.me/testbot?start=pos-XRP-USDT-S"], f"открыто: {links}")
    page.expect(not page.api("/api/", method="POST"), "вкладка послала POST")


@scenario("pos_no_key", "П.1: нет ключа — текст сервера, только журнал, без «позиций нет»")
async def pos_no_key(page: Page, url: str) -> None:
    def patch(data: Any) -> Any:
        return {**data, "positions": [], "exchange_error": POSITIONS_NO_KEY}

    await open_page(page, url, Setup(preset="hedge", rules=[
        Rule("/api/positions", "patch", patch=patch)]))
    s = await _to_positions(page)
    page.expect(POSITIONS_NO_KEY["message"] in (s.get("positions_note") or ""),
                f"нет текста «нет ключа»: {s.get('positions_note')!r}")
    page.expect(s.get("positions_empty") is None, "при сбое ключа — «позиций нет»")
    page.expect(len(s.get("journal") or []) == 2, f"журнал: {s.get('journal')}")


@scenario("pos_503", "П.1: 503 при загрузке — «Повторить»; в опросе — пометка, карточки остаются")
async def pos_503(page: Page, url: str) -> None:
    await open_page(page, url, Setup(preset="single", rules=[
        Rule("/api/positions", "status", status=503, body=EXCHANGE_DOWN, times=1)]))
    await page.click("#account-positions")
    await page.wait_for(lambda s: s["positions_state"] == EXCHANGE_DOWN["message"]
                        and s["positions_retry"], 10, "ошибка загрузки с «Повторить»")
    await page.click("#positions-retry")
    await page.wait_for(_loaded, 8, "вкладка после «Повторить»")
    page.intercept(Rule("/api/positions", "status", status=503, body=EXCHANGE_DOWN, times=1))
    s = await page.wait_for(lambda s: s["positions_stale"] is not None, 12, "пометка в опросе")
    page.expect(len(s.get("positions") or []) == 1, "карточка пропала при ошибке опроса")
    page.expect(EXCHANGE_DOWN["message"] in (s.get("positions_stale") or ""),
                "в пометке нет текста сервера")
    await page.wait_for(lambda s: s["positions_stale"] is None, 25, "пометка снята")


@scenario("pos_401", "П.1: 401 в опросе — «Сессия истекла», опрос встал")
async def pos_401(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    await _to_positions(page)
    page.intercept(Rule("/api/positions", "status", status=401,
                        body={"code": "UNAUTHORIZED", "message": "Сессия устарела"}))
    await page.wait_for(lambda s: s["error_title"] == "Сессия истекла", 10, "«Сессия истекла»")
    start = page.now()
    await page.pump(7)
    after = [r for r in page.requests if r.at >= start]
    page.expect(not after, f"после 401 запросов {len(after)}, ждали 0")


@scenario("pos_poll", "П.1: опрос не чаще 5 с; скрытая вкладка — 0 запросов; уход — опрос стоит")
async def pos_poll(page: Page, url: str) -> None:
    await open_page(page, url, Setup(preset="single", native=True))
    await page.js(FAKE_POPUP)
    await page.telegram_event("settings_button_pressed")
    await page.wait_for(_loaded, 10, "вкладка из меню")
    start = page.now()
    await page.pump(11)
    polled = _positions_requests(page, start)
    page.expect(1 <= len(polled) <= 3, f"/api/positions за 11 с: {len(polled)}, ждали 2")
    await page.set_hidden(True)
    await page.pump(0.5)
    start = page.now()
    await page.pump(7)
    page.expect(not [r for r in page.requests if r.at >= start], "скрытая вкладка — запросы")
    await page.set_hidden(False)
    await page.pump(1)
    page.expect(bool(_positions_requests(page, start)), "вкладка видна — опрос не возобновился")
    await page.telegram_event("back_button_pressed")
    await page.wait_for(lambda s: "screen-market" in s["screens"], 8, "«Назад» → рынок")
    start = page.now()
    await page.pump(7)
    page.expect(not _positions_requests(page, start), "ушли с вкладки — опрос позиций идёт")


@scenario("pos_to_market", "П.1: карточка → рынок её монеты, «Назад» → снова «Позиции»")
async def pos_to_market(page: Page, url: str) -> None:
    # Без монеты (открыт поиск): переход с карточки должен сам выбрать XRP-USDT.
    await open_page(page, url, Setup(native=True, preset="single", symbol=None,
                                     wait_screen=None))
    await page.wait_for(lambda s: s["search"], 10, "поиск монеты")
    await page.js(FAKE_POPUP)
    await page.telegram_event("settings_button_pressed")
    await page.wait_for(_loaded, 10, "вкладка из меню")
    await page.click('[data-position="XRP-USDT:LONG"] [data-symbol]')
    await page.wait_for(lambda s: "screen-market" in s["screens"] and s["symbol"] == SYMBOL
                        and s["market_charts"] == 1, 10, "рынок XRP-USDT")
    await page.telegram_event("back_button_pressed")
    await page.wait_for(_loaded, 8, "«Назад» → «Позиции»")


@scenario("menu_native", "П.1: SettingsButton (8.0) → showPopup: «Позиции», «Обновить»")
async def menu_native(page: Page, url: str) -> None:
    s = await open_page(page, url, Setup(native=True))
    page.expect(s.get("settings_button") is True, "SettingsButton не показан")
    page.expect(s.get("menu_link") is False, "при SettingsButton видна своя ссылка")
    await page.js(FAKE_POPUP)
    await page.telegram_event("settings_button_pressed")
    await page.wait_for(_loaded, 10, "«💼 Позиции» из меню")
    popups = await page.js("window.__popups")
    page.expect(len(popups) == 1 and [b.get("id") for b in popups[0]["buttons"]]
                == ["positions", "refresh", None], f"кнопки меню: {popups}")
    await page.js("window.__choice = 'refresh'")
    start = page.now()
    await page.telegram_event("settings_button_pressed")
    await page.pump(1.5)
    page.expect(bool(page.api("/api/me", since=start)), "«Обновить» не перечитал счёт")
    page.expect(bool(_positions_requests(page, start)), "«Обновить» не перечитал позиции")
    await page.telegram_event("back_button_pressed")
    await page.wait_for(lambda s: "screen-market" in s["screens"] and s["market_charts"] == 1,
                        8, "рынок")
    start = page.now()
    await page.telegram_event("settings_button_pressed")
    await page.pump(1.5)
    page.expect(bool(page.api(f"/api/market/{SYMBOL}?", since=start)),
                "«Обновить» на рынке не перечитал рынок")


SCENARIO_TIMEOUT = 150.0


async def run_one(transport: bc.Transport, url: str, item: Scenario,
                  reset: Callable[[], Awaitable[None]] | None = None,
                  seed: Callable[[str], Awaitable[None]] | None = None) -> Page:
    page = Page(transport)
    page.reset_stand = reset
    page.seed_stand = seed
    try:
        await asyncio.wait_for(item.fn(page, url), SCENARIO_TIMEOUT)
    except ScenarioError as exc:
        page.problems.append(f"сценарий прерван: {exc}")
    except TimeoutError:
        page.problems.append(f"сценарий не уложился в {SCENARIO_TIMEOUT:g} с")
    except Exception as exc:          # ошибка в самом сценарии — замечание, остальные идут
        page.problems.append(f"сценарий упал: {type(exc).__name__}: {exc}")
    return page


def select(name: str) -> list[Scenario]:
    if name == "all":
        return list(SCENARIOS.values())
    if name not in SCENARIOS:
        raise bc.EnvironmentNotReadyError(
            f"нет сценария {name!r}; есть: all, {', '.join(SCENARIOS)}")
    return [SCENARIOS[name]]


def render(item: Scenario, page: Page) -> str:
    issues = page.issues()
    head = f"{'✗' if issues else '✓'} {item.name} — {item.title}"
    lines = [head] + [f"    · {n}" for n in page.notes] + [f"    ✗ {i}" for i in issues]
    return "\n".join(lines)


__all__ = ["SCENARIOS", "STATE_JS", "Page", "Rule", "ScenarioError", "Setup", "open_page",
           "render", "run_one", "select"]
