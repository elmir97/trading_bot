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
import hashlib
import itertools
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from scripts import miniapp_browser_check as bc

SYMBOL = "XRP-USDT"
NATIVE_VERSION = "8.0"
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
    // Своя ссылка «Позиции» (без SettingsButton) — в деталях плашки (Д.3): есть ли она в меню.
    menu_link: !!$('account-positions') && !$('account-positions').hidden,
    details_open: shown('account-details'),
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
    lines: box && box.dataset.lines !== undefined
      ? box.dataset.lines.split(' ').filter(Boolean) : null,
    lines_on: box ? box.dataset.linesOn || null : null,
    lines_view: box && box.levelsInView ? box.levelsInView() : null,
    trade_lines_view: $('trade-chart') && $('trade-chart').levelsInView
      ? $('trade-chart').levelsInView() : null,
    market_position: shown('market-position')
      ? [...document.querySelectorAll('#market-position-list li')]
        .map((li) => li.textContent.trim()) : null,
    market_position_stale: shown('market-position-stale') ? text('market-position-stale') : null,
  };
})()"""


# Что показать в замечании «не дождался» — главное о странице.
WAIT_KEYS = ("screens", "last", "market_error", "chart_state", "stale", "error_title",
             "blocked", "lev", "lev_max", "pending", "preview_state", "confirm_card",
             "confirm_status", "confirm_action", "main_button", "positions_state",
             "positions_note", "positions_empty", "lines", "lines_on", "market_position")


SDK_FAILED = "SDK telegram.org не загрузился (сеть) — повтори сценарий"


class ScenarioError(Exception):
    """Сценарий не может продолжаться (страница не ответила, ошибка JS)."""


@dataclass
class Rule:
    """Подмена сети для запросов, чей адрес содержит `match` (exact — путь адреса равен
    `match`: «/api/open» не ловит «/api/open/preview»), и метод — `method`.

    action: "status" — ответ без сервера (status, body); "patch" — ответ сервера,
    JSON правит `patch`; "fail" — обрыв до сервера (net::ERR_INTERNET_DISCONNECTED);
    "hang" — запрос держится без ответа. times — сколько раз (None — всегда).
    raw — у "status" тело как есть, не JSON (пустое — как 502 Caddy без api);
    sdk — у "status" ответ локальной копией SDK telegram.org (bc.STAND_SDK)."""

    match: str
    action: str
    status: int = 200
    body: Any = None
    patch: Callable[[Any], Any] | None = None
    method: str | None = None
    times: int | None = None
    exact: bool = False
    raw: str | None = None
    sdk: bool = False
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
        elif rule.action == "status" and rule.sdk:
            await self.call("Fetch.fulfillRequest", bc.sdk_fulfill(rid))
        elif rule.action == "status" and rule.raw is not None:
            await self.call("Fetch.fulfillRequest", {
                "requestId": rid, "responseCode": rule.status, "responseHeaders": [],
                "body": base64.b64encode(rule.raw.encode()).decode()})
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
            if last and "screen-sdk" in last.get("screens", []):
                # Сеть до telegram.org, не код (как в open_page): исход — INCONCLUSIVE.
                raise ScenarioError(SDK_FAILED)
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
    init_data: bool = True             # заглушка tgWebAppData; False — как Safari по ссылке
    preset: str | None = None          # позиции стенда (miniapp_dev.PRESETS)
    theme: str | None = None           # "light" | "dark": themeParams в хеше + prefers-color-scheme
    real_sdk: bool = False             # SDK с telegram.org; по умолчанию — копия стенда (без сети)
    intercept_fonts: bool = False      # Fetch ловит и vendor/fonts (правила на шрифты)


# Тема клиента Telegram (стандартные цвета светлой и тёмной); вне Telegram страница видит
# только prefers-color-scheme — open_page задаёт и его, чтобы обе ветки совпадали.
THEME_PARAMS: dict[str, dict[str, str]] = {
    "light": {"bg_color": "#ffffff", "text_color": "#000000", "hint_color": "#999999",
              "link_color": "#2481cc", "button_color": "#2481cc",
              "button_text_color": "#ffffff", "secondary_bg_color": "#efeff3"},
    "dark": {"bg_color": "#17212b", "text_color": "#f5f5f5", "hint_color": "#708499",
             "link_color": "#6ab3f3", "button_color": "#5288c1",
             "button_text_color": "#ffffff", "secondary_bg_color": "#232e3c"},
}


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
    if setup.theme is not None:
        await page.call("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-color-scheme", "value": setup.theme}]})
    for rule in setup.rules:
        page.intercept(rule)
    if not setup.real_sdk:
        # После правил сценария: sdk_block/sdk_hang перекрывают копию своими правилами.
        page.intercept(Rule(bc.SDK_URL, "status", sdk=True))
    patterns = [{"urlPattern": "*/api/*", "requestStage": "Request"},
                {"urlPattern": "*telegram.org*", "requestStage": "Request"}]
    if setup.intercept_fonts:
        patterns.append({"urlPattern": "*/vendor/fonts/*", "requestStage": "Request"})
    await page.call("Fetch.enable", {"patterns": patterns})
    await page.call("Page.navigate", {"url": bc.page_url(
        url, version=NATIVE_VERSION if setup.native else None, init_data=setup.init_data,
        theme_params=None if setup.theme is None else THEME_PARAMS[setup.theme])})
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
        raise ScenarioError(SDK_FAILED)
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


# Фон графика — самый частый цвет полоски 40×1 у верха холста: в одну точку может попасть
# линия сетки (её место зависит от времени). Полоска — копией в свой холст: несколько
# getImageData с холста библиотеки Chrome отмечает предупреждением (willReadFrequently).
THEME_STATE_JS = """(() => {
  const c = document.querySelector('#market-chart canvas');
  let pixel = [];
  if (c) {
    const strip = document.createElement('canvas');
    strip.width = 40;
    strip.height = 1;
    const ctx = strip.getContext('2d');
    ctx.drawImage(c, 3, 3, 40, 1, 0, 0, 40, 1);
    const d = ctx.getImageData(0, 0, 40, 1).data;
    const seen = {};
    for (let i = 0; i < d.length; i += 4) {
      const key = d[i] + ',' + d[i + 1] + ',' + d[i + 2];
      seen[key] = (seen[key] || 0) + 1;
    }
    const top = Object.entries(seen).sort((a, b) => b[1] - a[1])[0][0];
    pixel = top.split(',').map(Number);
  }
  return { theme: document.documentElement.dataset.theme || null,
           body: getComputedStyle(document.body).backgroundColor, pixel };
})()"""
# Бумага и карточка набора (style.css, таблица §9а): фон страницы и фон графика.
THEME_RGB = {"light": ("rgb(246, 246, 242)", [255, 255, 255]),
             "dark": ("rgb(20, 22, 26)", [27, 30, 36])}


async def _expect_theme(page: Page, want: str) -> None:
    got = await page.js(THEME_STATE_JS)
    body, pixel = THEME_RGB[want]
    page.expect(got["theme"] == want, f"data-theme {got['theme']!r}, ждали {want}")
    page.expect(got["body"] == body, f"{want}: фон страницы {got['body']}, ждали {body}")
    page.expect(got["pixel"] == pixel, f"{want}: фон графика {got['pixel']}, ждали {pixel}")


@scenario("theme", "Д.2: theme_changed — страница и график в наборе colorScheme (светлая ↔ тёмная)")
async def theme(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True, theme="light"))
    await _expect_theme(page, "light")
    for want in ("dark", "light"):
        await page.telegram_event("theme_changed", {"theme_params": THEME_PARAMS[want]})
        await page.pump(1)
        await _expect_theme(page, want)


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
    page.expect((_button(s, native) or "").startswith("Открыть лонг · "),
                f"кнопка: {_button(s, native)!r}")
    page.expect(int(s.get("lev") or 0) <= cap, f"плечо {s.get('lev')} выше потолка {cap}")
    page.expect(s.get("take_2r_disabled") is False, "чип 2R неактивен при стопе")

    await page.click("#trade-take-2r")
    s = await _settled(page, "превью 2R")
    page.expect("RR1:2.00" in (s.get("result") or "").replace(" ", ""), f"2R: {s.get('result')!r}")
    # Автомасштаб (17.10): вход, стоп и тейк — в видимом диапазоне графика формы.
    await page.wait_for(lambda s: _in_view(s.get("trade_lines_view")), 5,
                        "вход/стоп/тейк формы в видимом диапазоне")
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
    page.expect((_button(s, native) or "").startswith("Открыть лонг · "),
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
    """Главная кнопка экрана подтверждения; без showConfirm (6.0) — два нажатия, второе не
    раньше SECOND_PRESS_MS (400 мс) после первого."""
    if native:
        await page.telegram_event("main_button_pressed")
    else:
        await page.click("#confirm-action")
        await page.pump(SECOND_PRESS)
        await page.click("#confirm-action")


def _done(s: dict[str, Any]) -> bool:
    return "Сделка #" in (s.get("confirm_card") or "")


async def _open_and_confirm(page: Page, native: bool) -> dict[str, Any]:
    s = await _to_confirm(page, native)
    page.expect((s.get("confirm_timer") or "").startswith("действует "),
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


async def _positions_link(page: Page) -> None:
    """Своя ссылка «💼 Позиции» (браузер 6.0 — SettingsButton нет) — в деталях плашки (Д.3)."""
    await page.click("#account-plate")
    await page.wait_for(lambda s: s["details_open"], 3, "детали плашки")
    await page.click("#account-positions")


async def _to_positions(page: Page) -> dict[str, Any]:
    await _positions_link(page)
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


@scenario("pos_chat", "П.1: «Действия →» — openTelegramLink t.me/<бот>?start=pos-…")
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
    await open_page(page, url, Setup(preset="single"))
    # Рынок сам читает позиции (линии П.2) — подмена после его запроса, иначе он её съест.
    await page.wait_for(lambda s: bool(s["lines"]), 10, "линии позиции на рынке")
    page.intercept(Rule("/api/positions", "status", status=503, body=EXCHANGE_DOWN, times=1))
    await _positions_link(page)
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
    # Рынок читает позиции сам (линии П.2): один раз при загрузке, дальше раз в 10 с.
    after = _positions_requests(page, start)
    page.expect(len(after) <= 1, f"ушли с вкладки — /api/positions за 7 с: {len(after)}, "
                                 "ждали ≤ 1 (линии рынка)")


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


# П.2 — линии открытой позиции на экране рынка

HEDGE_LINES = {"entry:1.4810", "stop:1.4402", "stop:1.4100", "take:1.5631", "entry:1.5120"}


def _lines(s: dict[str, Any]) -> set[str]:
    return set(s.get("lines") or [])


def _in_view(view: list[str] | None) -> bool:
    """Вход, стоп, тейк есть и все в видимом диапазоне цены; ликвидация — любая."""
    scaled = [v for v in view or [] if v.split(":")[0] in ("entry", "stop", "take")]
    kinds = {v.split(":")[0] for v in scaled}
    return kinds == {"entry", "stop", "take"} and all(v.endswith(":in") for v in scaled)


@scenario("market_lines", "П.2: хедж — линии входа/стопов/тейка/ликв. на свечах и на «линии», "
                          "закрыта — сняты")
async def market_lines(page: Page, url: str) -> None:
    await open_page(page, url, Setup(preset="hedge"))
    s = await page.wait_for(lambda s: _lines(s) >= HEDGE_LINES, 10, "линии позиции")
    page.expect(s.get("lines_on") == "candles", f"линии на {s.get('lines_on')!r}, ждали candles")
    # Автомасштаб: тейк 1.5631 и нижний стоп 1.4100 за краем свечей — график их включает.
    s = await page.wait_for(lambda s: _in_view(s.get("lines_view")), 5,
                            "вход/стопы/тейк в видимом диапазоне")
    legend = s.get("market_position") or []
    page.expect(all("ликв." in x for x in legend), f"нет ликвидации строкой: {legend}")
    page.expect(sum(x.startswith("liq:") for x in _lines(s)) == 2,
                f"ликвидаций: {sorted(_lines(s))}")
    legend = s.get("market_position") or []
    page.expect(len(legend) == 2 and legend[0].startswith("Лонг 195")
                and "без стопа" in legend[1] and "без стопа" not in legend[0],
                f"блок «Позиция»: {legend}")
    await page.click('#market-types [data-type="line"]')
    s = await page.wait_for(lambda s: s["lines_on"] == "line" and _in_view(s.get("lines_view")),
                            5, "линии на «линии», в видимом диапазоне")
    page.expect(_lines(s) >= HEDGE_LINES, f"на «линии» уровни: {s.get('lines')}")
    await page.click('#market-types [data-type="candles"]')
    await page.wait_for(lambda s: s["lines_on"] == "candles" and _lines(s) >= HEDGE_LINES, 5,
                        "обратно на свечи")
    # Позиции закрыты на бирже — следующий опрос (≤ 10 с + кэш 5 с) снимает линии и блок.
    if page.seed_stand is None:
        raise ScenarioError("сценарию нужен стенд (/dev/positions)")
    await page.seed_stand("none")
    start = page.now()
    s = await page.wait_for(lambda s: s["lines"] == [] and s["market_position"] is None, 18,
                            "линии сняты")
    polled = _positions_requests(page, start)
    page.expect(1 <= len(polled) <= 2, f"/api/positions до снятия: {len(polled)}, ждали 1–2")


@scenario("market_lines_none", "П.2: нет ключа — линий и блока нет, рынок работает")
async def market_lines_none(page: Page, url: str) -> None:
    def patch(data: Any) -> Any:
        return {**data, "positions": [], "exchange_error": POSITIONS_NO_KEY}

    await open_page(page, url, Setup(preset="single", rules=[
        Rule("/api/positions", "patch", patch=patch)]))
    await page.pump(2)
    s = await page.state()
    page.expect(s.get("lines") == [] and s.get("market_position") is None,
                f"без ключа: линии {s.get('lines')}, блок {s.get('market_position')}")
    page.expect(s.get("last") not in (None, "—"), "рынок не показан")


@scenario("confirm_lines", "П.2: «Открыть» → «К рынку» — свежий /api/positions, линии входа, стопа")
async def confirm_lines(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    s = await page.state()
    page.expect(s.get("lines") == [], f"до сделки линии: {s.get('lines')}")
    await _ready_form(page, False)
    await _open_and_confirm(page, False)
    start = page.now()
    await page.click("#confirm-back")
    s = await page.wait_for(lambda s: "screen-market" in s["screens"]
                            and any(x.startswith("entry:") for x in _lines(s))
                            and any(x.startswith("stop:") for x in _lines(s)), 8,
                            "линии новой позиции")
    page.expect(bool(_positions_requests(page, start)), "после итога нет запроса позиций")
    page.expect(len(s.get("market_position") or []) == 1, f"блок: {s.get('market_position')}")

# Вне Telegram и прокси без api (17.10, Safari по ссылке на проде)

OUTSIDE_TITLE = "Открой Mini App из бота @postfactumTrade_bot"


@scenario("no_init_data", "Вне Telegram (initData пустая) — «Открой из бота», к API не ходит")
async def no_init_data(page: Page, url: str) -> None:
    s = await open_page(page, url, Setup(init_data=False, wait_screen="screen-error"))
    page.expect(s.get("error_title") == OUTSIDE_TITLE, f"заголовок: {s.get('error_title')!r}")
    await page.pump(2)
    s = await page.state()
    page.expect(not page.api("/api/"), f"запросы к API: {[r.path for r in page.requests]}")
    retry = await page.js("document.getElementById('error-retry').hidden")
    page.expect(retry is True, "на экране «Открой из бота» есть «Повторить»")
    page.expect(s.get("fallback") is None and s.get("main_button") is None,
                "кнопки «Лонг»/«Шорт» видны вне Telegram")


@scenario("me_502", "Прокси без api: /api/me 502 без JSON — «Сервер недоступен», повтор открывает")
async def me_502(page: Page, url: str) -> None:
    await open_page(page, url, Setup(rules=[
        Rule("/api/me", "status", status=502, raw="", exact=True, times=1)],
        wait_screen="screen-error"))
    s = await page.state()
    page.expect(s.get("error_title") == "Не получилось", f"заголовок: {s.get('error_title')!r}")
    text = await page.js("document.getElementById('error-text').textContent")
    page.expect(text == "Сервер недоступен — повтори через минуту.", f"текст: {text!r}")
    await page.click("#error-retry")
    await page.wait_for(lambda s: "screen-market" in s["screens"] and s["market_charts"] == 1,
                        10, "рынок после «Повторить»")



@scenario("sdk_real", "SDK с telegram.org (не копия стенда): страница стартует, рынок с графиком")
async def sdk_real(page: Page, url: str) -> None:
    s = await open_page(page, url, Setup(real_sdk=True))
    page.expect(s.get("market_charts") == 1, f"рынок с настоящим SDK: {s.get('screens')}")
    sdk = [u for st, u in page.collector.network if u == bc.SDK_URL and st == 200]
    page.expect(bool(sdk), "SDK не пришёл с telegram.org")
    # Копия стенда = настоящий SDK? Расхождение — WARN в отчёте, гейт не валит (владелец 18.10).
    rid = next((r for r, u in page.collector.urls.items() if u == bc.SDK_URL), None)
    if rid is None:
        page.note("WARN: SDK не сверен с копией стенда — запрос не найден")
        return
    got = await page.call("Network.getResponseBody", {"requestId": rid})
    raw = got.get("body", "")
    real = base64.b64decode(raw) if got.get("base64Encoded") else raw.encode("utf-8")
    page.note(sdk_compare(hashlib.sha256(real).hexdigest(),
                          hashlib.sha256(bc.STAND_SDK.read_bytes()).hexdigest()))


def sdk_compare(real: str, copy: str) -> str:
    """Строка отчёта sdk_real: совпала копия стенда с telegram.org или нет (WARN)."""
    if real == copy:
        return f"SDK telegram.org = копия стенда (sha256 {copy[:12]}…)"
    return (f"WARN: SDK telegram.org ({real[:12]}…) ≠ копия стенда ({copy[:12]}…) — обновить "
            "scripts/stand_sdk/telegram-web-app.js и sha256 в tests/test_miniapp_browser_check.py")


# Д — дизайн Mini App (§9а «Дизайн Mini App»)

FONT_FACES = (("Golos Text", "400", "Жж"), ("Golos Text", "800", "Az"),
              ("IBM Plex Mono", "400", "1.2974 объём"), ("IBM Plex Mono", "500", "Az Жж"),
              ("IBM Plex Mono", "600", "1 683.45 $"), ("Caveat", "600", "как в плане 1:2"))
FONTS_JS = """(async () => {
  const faces = %s;
  const out = {};
  for (const [family, weight, text] of faces) {
    const spec = weight + ' 16px "' + family + '"';
    const loaded = await document.fonts.load(spec, text);
    out[family + ' ' + weight] = loaded.length > 0 && document.fonts.check(spec, text);
  }
  const r = await fetch('vendor/fonts/ibm-plex-mono-5.3.0/ibm-plex-mono-latin-400-normal.woff2');
  return { faces: out, body: getComputedStyle(document.body).fontFamily,
           woff2_type: r.headers.get('content-type'), woff2_status: r.status };
})()"""


@scenario("design_fonts", "Д.1: шрифты Golos/Plex Mono/Caveat — из vendor, body — Golos")
async def design_fonts(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    got = await page.js(FONTS_JS % json.dumps(FONT_FACES))
    missing = [k for k, ok in got["faces"].items() if not ok]
    page.expect(not missing, f"шрифты не загрузились: {missing}")
    page.expect(got["body"].startswith('"Golos Text"'), f"body: {got['body']!r}")
    page.expect(got["woff2_status"] == 200 and got["woff2_type"] == "font/woff2",
                f"woff2: {got['woff2_status']} {got['woff2_type']!r}")
    fonts = [(s, u) for s, u in page.collector.network if u.endswith(".woff2")]
    page.note(f"woff2 загружено: {len(fonts)}")
    page.expect(bool(fonts) and all(s == 200 and u.startswith(url) for s, u in fonts),
                f"woff2 не свои или не 200: {fonts}")


# События клиенту Telegram (postEvent) — в window.__events, до смены темы.
RECORD_EVENTS = ("window.__events = []; const post = Telegram.WebView.postEvent;"
                 " Telegram.WebView.postEvent = (type, cb, data) => {"
                 " window.__events.push([type, data || null]); return post(type, cb, data); }")
DESIGN_JS = """(async () => {
  const { groupDigits } = await import('./render.js');
  const tg = Telegram.WebApp;
  const last = (type) => {
    const found = window.__events.filter((e) => e[0] === type);
    return found.length ? found[found.length - 1][1] : null;
  };
  const hands = [...document.querySelectorAll('.hand')].filter((e) => e.offsetParent).length;
  return {
    header: last('web_app_set_header_color'),
    background: last('web_app_set_background_color'),
    bottom: last('web_app_set_bottom_bar_color'),
    main: [tg.MainButton.color, tg.MainButton.textColor],
    secondary: [tg.SecondaryButton.color, tg.SecondaryButton.textColor],
    price_font: getComputedStyle(document.getElementById('market-last')).fontFamily,
    hands,
    digits: ['84172.35', '1683.45', '-1683.45', '956', '1234567', '—', null, '1.2974',
             '12a'].map(groupDigits),
  };
})()"""
NB = chr(160)                     # неразрывный пробел groupDigits
DIGITS = ["84" + NB + "172.35", "1" + NB + "683.45", "-1" + NB + "683.45", "956",
          "1" + NB + "234" + NB + "567", "—", "", "1.2974", "12a"]


@scenario("design_theme", "Д.2: шапка/фон/нижняя полоса Telegram и нативные кнопки — в цветах "
                          "набора; разряды; цена моноширинным")
async def design_theme(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True, theme="light"))
    await page.js(RECORD_EVENTS)
    for want, paper, ink, red in (("dark", "#14161a", "#ece9e1", "#ff6b5e"),
                                  ("light", "#f6f6f2", "#16181d", "#d43329")):
        await page.telegram_event("theme_changed", {"theme_params": THEME_PARAMS[want]})
        await page.pump(1)
        got = await page.js(DESIGN_JS)
        for key in ("header", "background", "bottom"):
            page.expect(got[key] == {"color": paper}, f"{want}: {key} {got[key]!r}, ждали {paper}")
        page.expect(got["main"] == [ink, paper], f"{want}: MainButton {got['main']}")
        page.expect(got["secondary"] == [paper, red], f"{want}: SecondaryButton {got['secondary']}")
    page.expect(got["price_font"].startswith('"IBM Plex Mono"'), f"цена: {got['price_font']!r}")
    page.expect(got["hands"] <= 1, f"пометок «от руки» на экране {got['hands']}")
    page.expect(got["digits"] == DIGITS, f"groupDigits: {got['digits']}")


# Д.3 — экран рынка: шапка B, цена, график в рамке, детали

MARKET_DESIGN_JS = """(() => {
  const $ = (id) => document.getElementById(id);
  const t = (e) => e ? e.textContent.replace(/\\s+/g, ' ').trim() : null;
  const box = document.querySelector('#screen-market .chart-box');
  const plate = $('account-plate').getBoundingClientRect();
  return {
    plate_in_head: $('market-account').contains($('account')),
    plate_in_topbar: $('topbar').contains($('account')),
    topbar_shown: !$('topbar').hidden,
    plate: t($('account-plate')),
    plate_right: Math.round(plate.right),
    live: $('account').classList.contains('live'),
    mode: t($('account-mode')),
    mode_shown: !!$('account-mode').offsetParent,
    mode_bg: getComputedStyle($('account-mode')).backgroundColor,
    plate_border: getComputedStyle($('account-plate')).borderTopColor,
    red: getComputedStyle(document.documentElement).getPropertyValue('--red').trim(),
    red_text: getComputedStyle(document.documentElement).getPropertyValue('--red-text').trim(),
    sub: t(document.querySelector('.symbol-sub')),
    symbol_clipped: $('market-symbol').scrollWidth > $('market-symbol').clientWidth,
    price_size: getComputedStyle($('market-last')).fontSize,
    chart_border: box ? [getComputedStyle(box).borderTopStyle, getComputedStyle(box).borderTopColor,
      getComputedStyle($('market-last')).color] : null,
    time: t($('market-time')),
    details: $('account-details').hidden ? null
      : [...$('account-details').querySelectorAll('.receipt-row')]
        .map((row) => [...row.children].map(t).join(' ')).join(' | '),
    width: document.documentElement.clientWidth,
    scroll_x: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
})()"""


async def _market_design(page: Page) -> dict[str, Any]:
    got = await page.js(MARKET_DESIGN_JS)
    if not isinstance(got, dict):
        raise ScenarioError(f"шапка не прочитана: {got!r}")
    return got


def _rgb(hex_: str) -> str:
    r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgb({r}, {g}, {b})"


def _grouped(value: str) -> str:
    """Как groupDigits, но пробел обычный (\\s+ в MARKET_DESIGN_JS сводит неразрывный)."""
    whole, _, frac = value.partition(".")
    out = ""
    while len(whole) > 3:
        out = " " + whole[-3:] + out
        whole = whole[:-3]
    return whole + out + ("." + frac if frac else "")


@scenario("design_market", "Д.3: шапка B — плашка «DEMO · equity» в строке монеты, детали по "
                           "нажатию, «до N×», цена 42px, график в рамке, 320 px; на «Позициях» — "
                           "в верхней строке")
async def design_market(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    me = await page.js("fetch('/api/me').then((r) => r.json())")
    dry = bool(me["flags"]["dry_run"])
    g = await _market_design(page)
    page.expect(g["plate_in_head"] and not g["topbar_shown"], f"плашка не в строке монеты: {g}")
    page.expect(g["plate"] == f"DEMO {_grouped(me['equity'])} $",
                f"плашка: {g['plate']!r} (equity {me['equity']})")
    page.expect(g["sub"] == "фьючерс · до 125×", f"подпись монеты: {g['sub']!r}")
    page.expect(g["price_size"] == "42px", f"цена {g['price_size']}")
    # Рамка 1.5px — при плотности 1 Chrome рисует 1px; проверяем, что она сплошная чернилами.
    style, color, ink = g["chart_border"] or ("", "", "-")
    page.expect(style == "solid" and color == ink, f"рамка графика {g['chart_border']}")
    want_time = "сухой прогон — на биржу не уйдёт ·" if dry else "Цена на"
    page.expect((g["time"] or "").startswith(want_time), f"строка под ценой: {g['time']!r}")
    page.expect(g["details"] is None, "детали плашки открыты без нажатия")
    await page.click("#account-plate")
    g = await _market_design(page)
    details = g["details"] or ""
    for part in (f"equity {me['equity']}", f"свободно {me['available']}", "счёт DEMO",
                 "сухой прогон " + ("да — на биржу не уйдёт" if dry else "нет")):
        page.expect(part in details, f"в деталях нет «{part}»: {details!r}")
    page.expect("🧪" not in (g["plate"] or ""), "🧪 в плашке")
    await page.click("#market-last")                 # нажатие мимо — детали закрываются
    g = await _market_design(page)
    page.expect(g["details"] is None, "детали не закрылись нажатием мимо")
    await page.viewport(320, 844)
    await page.pump(1)
    g = await _market_design(page)
    page.expect(g["scroll_x"] == 0 and g["plate_right"] <= g["width"],
                f"320 px: прокрутка {g['scroll_x']}, плашка до {g['plate_right']} при {g['width']}")
    page.expect(not g["symbol_clipped"], "320 px: монета обрезана («XRP-…»)")
    await page.viewport(390, 844)
    await _to_positions(page)
    g = await _market_design(page)
    page.expect(g["plate_in_topbar"] and g["topbar_shown"], f"на «Позициях» плашки нет: {g}")


@scenario("design_live", "Д.3: LIVE — плашка красная с «⚠️ Реальный счёт», 320 px без прокрутки")
async def design_live(page: Page, url: str) -> None:
    await open_page(page, url, Setup(width=320, rules=[Rule("/api/me", "patch", patch=_live)]))
    g = await _market_design(page)
    page.expect(g["live"] and g["mode"] == "⚠️ Реальный счёт" and g["mode_shown"],
                f"LIVE: {g['mode']!r} {g['live']} видно {g['mode_shown']}")
    page.expect(g["mode_bg"] == _rgb(g["red_text"]) and g["plate_border"] == _rgb(g["red"]),
                f"LIVE не красная: ярлык {g['mode_bg']}, рамка {g['plate_border']}")
    page.expect(g["scroll_x"] == 0 and g["plate_right"] <= g["width"],
                f"LIVE 320 px: прокрутка {g['scroll_x']}, плашка до {g['plate_right']}")
    await page.click("#account-plate")
    g = await _market_design(page)
    page.expect("счёт LIVE · реальный счёт" in (g["details"] or ""), f"детали: {g['details']!r}")


# Сдвиг при подмене шрифта (Д.3, владелец 18.10): у Plex нет preload (на экранах ошибок он
# дал бы предупреждение). Подмена — как у пользователя: --mono то запасной моно, то Plex
# (CSSOM, CSP её не режет; перезагрузка не годится — Chrome берёт шрифт из кэша в памяти).
# Вертикаль (top, height) — строго ≤ 1px; горизонталь (ширина цены, сдвиг подписи) —
# замер в note: по нему решение, грузить ли Plex заранее (после /api/me).
SWAP_JS = """(async () => {
  await document.fonts.ready;
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const r = (sel) => {
    const e = document.querySelector(sel);
    if (!e) return null;
    const b = e.getBoundingClientRect();
    return [Math.round(b.top), Math.round(b.height), Math.round(b.left), Math.round(b.width)];
  };
  return {
    family: getComputedStyle(document.getElementById('market-last')).fontFamily,
    head: r('#market-head'), price: r('#market-last'), change: r('#market-change'),
    time: r('#market-time'), chart: r('#screen-market .chart-box'),
    controls: r('#screen-market .chart-controls'), details: r('#screen-market .details-lines'),
    plate: r('#account-plate'),
  };
})()"""
FALLBACK_MONO = "ui-monospace, Menlo, Consolas, monospace"
SWAP_KEYS = ("head", "price", "change", "time", "chart", "controls", "details", "plate")
PLEX_CHECK_JS = "document.fonts.check('600 42px \"IBM Plex Mono\"', '1.4950')"


@scenario("design_font_swap", "Д.3: подмена шрифта не двигает рынок — геометрия с запасным моно = "
                              "с Plex")
async def design_font_swap(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    first = await page.js(PLEX_CHECK_JS)
    page.note(f"Plex к первому показу цены: {'загружен' if first else 'ещё нет'}")
    # Plex запрошен после /api/me и до первого /api/market (app.js loadNumberFont).
    order = list(page.collector.urls.values())
    plex_at = next((i for i, u in enumerate(order) if "ibm-plex-mono-latin-600" in u), None)
    me_at = next((i for i, u in enumerate(order) if u.endswith("/api/me")), None)
    market_at = next((i for i, u in enumerate(order) if "/api/market/" in u), None)
    in_order = (plex_at is not None and me_at is not None and market_at is not None
                and me_at < plex_at < market_at)
    page.expect(in_order,
                f"порядок запросов: /api/me {me_at}, Plex {plex_at}, /api/market {market_at}")
    await page.pump(1)
    plex = await page.js(SWAP_JS)
    page.expect(plex["family"].startswith('"IBM Plex Mono"') and await page.js(PLEX_CHECK_JS),
                f"Plex не загрузился: {plex['family']!r}")
    await page.js("document.documentElement.style.setProperty('--mono', "
                  f"{json.dumps(FALLBACK_MONO)})")
    fallback = await page.js(SWAP_JS)
    await page.js("document.documentElement.style.removeProperty('--mono')")
    page.expect(not fallback["family"].startswith('"IBM Plex Mono"'),
                f"подмена не сработала: {fallback['family']!r}")
    moved = {k: (fallback[k][:2], plex[k][:2]) for k in SWAP_KEYS
             if fallback[k] is None or plex[k] is None
             or any(abs(a - b) > 1 for a, b in zip(fallback[k][:2], plex[k][:2], strict=True))}
    page.expect(not moved, f"сдвиг по вертикали при подмене шрифта [top, height]: {moved}")
    if fallback["price"] and plex["price"] and fallback["change"] and plex["change"]:
        page.note(f"ширина цены {fallback['price'][3]} → {plex['price'][3]} px, подпись изменения "
                  f"left {fallback['change'][2]} → {plex['change'][2]} px")

FONT_STATE_JS = """(() => ({
  plex: document.fonts.check('600 42px "IBM Plex Mono"', '1.4950'),
  price_visible: document.getElementById('market-last').getBoundingClientRect().width > 0,
}))()"""


async def _market_without_plex(page: Page, url: str, rule: Rule) -> None:
    """Plex недоступен — рынок всё равно показан запасным моно: loadNumberFont не ждётся,
    отказ шрифта не ломает экран (решение владельца 18.10)."""
    s = await open_page(page, url, Setup(intercept_fonts=True, rules=[rule]))
    page.expect(s.get("last") not in (None, "—") and s.get("market_charts") == 1,
                f"рынок без Plex не показан: {s.get('screens')} {s.get('last')!r}")
    await page.pump(3)                    # опрос цены идёт, шрифт так и не пришёл
    s = await page.state()
    page.expect(s.get("last") not in (None, "—"), "цена пропала")
    got = await page.js(FONT_STATE_JS)
    page.expect(got["plex"] is False, "Plex загрузился — правило не сработало")
    page.expect(got["price_visible"], "цена не видна (шрифт держит текст невидимым)")
    page.expect(rule.hits > 0, "запросов к Plex не было")


@scenario("design_font_404", "Д.4: Plex отвечает 404 — рынок и цена отрисованы запасным моно")
async def design_font_404(page: Page, url: str) -> None:
    await _market_without_plex(page, url, Rule("ibm-plex-mono", "status", status=404))


@scenario("design_font_hang", "Д.4: Plex висит без ответа — рынок и цена отрисованы запасным моно")
async def design_font_hang(page: Page, url: str) -> None:
    await _market_without_plex(page, url, Rule("ibm-plex-mono", "hang"))



# Д.4 — форма сделки: квитанция и кнопка — строки ответа preview, фронт только форматирует

# Объём в ответе preview подменяется значением, которое НЕ следует из суммы и цены входа:
# любой пересчёт на фронте (сумма / вход и т. п.) даст другое число — сценарий его поймает;
# заодно ≥ 1000 — проверка разрядов на кнопке («86 482»).
TAP_QUANTITY = "86482"


class PreviewTap:
    """Ответы preview сервера (правило Fetch «patch»; объём — TAP_QUANTITY) — эталон для
    квитанции: страница обязана показать те же строки, только с разрядами."""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def __call__(self, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("calc"), dict):
            data["calc"]["quantity"] = TAP_QUANTITY
        self.seen.append(data)
        return data

    @property
    def last(self) -> dict[str, Any]:
        if not self.seen or not isinstance(self.seen[-1], dict) or not self.seen[-1].get("calc"):
            raise ScenarioError(f"ответ preview не пойман: {self.seen[-1:]!r}")
        return self.seen[-1]


FORM_DESIGN_JS = """(() => {
  const $ = (id) => document.getElementById(id);
  const t = (id) => $(id).textContent.replace(/\\s+/g, ' ').trim();
  const color = (sel) => getComputedStyle(document.querySelector(sel)).color;
  const root = getComputedStyle(document.documentElement);
  const tg = window.Telegram && window.Telegram.WebApp;
  const picked = (id) => [...document.querySelectorAll(id + ' .selected')]
    .map((b) => b.textContent);
  return {
    receipt: ['trade-quantity', 'trade-notional', 'trade-margin-label', 'trade-margin',
              'trade-risk-usd', 'trade-rr', 'trade-liq', 'trade-fee'].map(t),
    hint: t('trade-risk-hint'),
    hint_hand: $('trade-risk-hint').classList.contains('hand'),
    hands: [...document.querySelectorAll('#screen-trade .hand')].filter((e) => e.offsetParent
      && e.textContent.trim()).length,
    stop_chip: picked('#trade-stop-chips'),
    take_chip: picked('#trade-take-chips'),
    stop_label: color('label[for="trade-stop"]'), take_label: color('label[for="trade-take"]'),
    red_text: root.getPropertyValue('--red-text').trim(),
    blue: root.getPropertyValue('--blue').trim(),
    main: tg && tg.MainButton.isVisible
      ? [tg.MainButton.text.replace(/\\s+/g, ' '), tg.MainButton.color, tg.MainButton.textColor]
      : null,
    paper: root.getPropertyValue('--paper').trim(), ink: root.getPropertyValue('--ink').trim(),
    scroll_x: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
})()"""


def _receipt_want(calc: dict[str, Any], margin_type: str) -> list[str]:
    """Что обязана показать квитанция: строки сервера + разряды + подписи. Здесь тоже
    ничего не считается — только форматирование тех же строк."""
    g = _grouped
    liq = "—" if calc["liq_estimate"] is None else (
        f"≈ {g(calc['liq_estimate'])}"
        + ("" if calc["liq_ratio"] is None else f" · {calc['liq_ratio']}× дальше стопа"))
    margin = {"ISOLATED": "маржа, изол.", "CROSSED": "маржа, кросс"}.get(margin_type, "маржа")
    return [f"{g(calc['quantity'])} XRP", f"{g(calc['notional'])} $", margin,
            f"{g(calc['margin'])} $", f"{g(calc['risk_usd'])} $ · {calc['risk_percent']}%",
            "—" if calc["rr"] is None else f"1 : {calc['rr']}", liq, f"≈ {g(calc['fee_usd'])} $"]


async def _form_design(page: Page) -> dict[str, Any]:
    got = await page.js(FORM_DESIGN_JS)
    if not isinstance(got, dict):
        raise ScenarioError(f"форма не прочитана: {got!r}")
    return got


@scenario("design_form", "Д.4: форма — квитанция и «Открыть лонг · N XRP» из ответа preview "
                         "(только разряды), чипы, подписи стоп/тейк, пометка риска, шорт, 320 px")
async def design_form(page: Page, url: str) -> None:
    tap = PreviewTap()
    await open_page(page, url, Setup(native=True, rules=[
        Rule("/api/open/preview", "patch", patch=tap, method="POST")]))
    me = await page.js("fetch('/api/me').then((r) => r.json())")
    await _to_form(page, True)
    await page.click('#trade-stop-chips [data-percent="2"]')
    await _settled(page, "превью формы")
    await page.pump(0.3)
    g = await _form_design(page)
    calc = tap.last["calc"]
    want = _receipt_want(calc, me["margin_type"])
    page.expect(g["receipt"] == want, f"квитанция {g['receipt']} ≠ сервер {want}")
    paper, ink, red = g["paper"], g["ink"], g["red_text"]
    page.expect(g["main"] == [f"Открыть лонг · {_grouped(calc['quantity'])} XRP", ink, paper],
                f"MainButton {g['main']}")
    page.expect(g["receipt"][0] == "86 482 XRP", f"объём не строка сервера: {g['receipt'][0]!r}")
    page.expect(g["stop_chip"] == ["2%"] and g["take_chip"] == ["нет"],
                f"выбранные чипы: стоп {g['stop_chip']}, тейк {g['take_chip']}")
    page.expect(g["stop_label"] == _rgb(red) and g["take_label"] == _rgb(g["blue"]),
                f"подписи: стоп {g['stop_label']}, тейк {g['take_label']}")
    page.expect(g["hint_hand"] and g["hint"].startswith(f"= {_grouped(calc['risk_usd'])} $"),
                f"пометка риска: {g['hint']!r}")
    page.expect(g["hands"] <= 1, f"пометок «от руки» на форме {g['hands']}")

    await page.click('#trade-side [data-side="SHORT"]')
    await page.pump(0.5)
    await _settled(page, "превью шорта")
    await page.pump(0.3)
    g = await _form_design(page)
    calc = tap.last["calc"]
    page.expect(g["main"] == [f"Открыть шорт · {_grouped(calc['quantity'])} XRP",
                              paper, red], f"MainButton шорта {g['main']}")

    await page.viewport(320, 844)
    await page.pump(1)
    g = await _form_design(page)
    page.expect(g["scroll_x"] == 0, f"форма 320 px: прокрутка {g['scroll_x']}")



# Д.5 — «ЗАПИСЬ» подтверждения: номер, строки и «было → стало» — только строки сервера

# Старый клиент: второе нажатие — не раньше SECOND_PRESS_MS (confirm.js) после первого.
SECOND_PRESS = 0.45

# Объём карточки ≠ объёма превью (TAP_QUANTITY) — «было → стало».
CARD_QUANTITY = "86510"


class CardTap:
    """Ответ POST /api/open (правило «patch»): объём карточки — CARD_QUANTITY; ответ
    сохраняется — эталон номера записи, срока и строк."""

    def __init__(self, change: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.seen: list[dict[str, Any]] = []
        self.change = change

    def __call__(self, data: Any) -> Any:
        if isinstance(data, dict):
            card = data.get("card") or {}
            if isinstance(card.get("calc"), dict):
                card["calc"]["quantity"] = CARD_QUANTITY
            if self.change is not None:
                self.change(data)
            self.seen.append(data)
        return data

    @property
    def last(self) -> dict[str, Any]:
        if not self.seen:
            raise ScenarioError("ответ /api/open не пойман")
        return self.seen[-1]


RECORD_JS = """(() => {
  const $ = (id) => document.getElementById(id);
  const t = (id) => $(id).textContent.replace(/\\s+/g, ' ').trim();
  const rows = [...document.querySelectorAll('#confirm-card .receipt-row')]
    .map((r) => [...r.children].map((c) => c.textContent.replace(/\\s+/g, ' ').trim()));
  return {
    number: t('confirm-number'), sub: t('confirm-sub'), title: t('confirm-title'),
    timer: $('confirm-timer').hidden ? null : t('confirm-timer'),
    rows,
    diff: $('confirm-diff-box').hidden ? null
      : [...document.querySelectorAll('#confirm-diff li')].map((li) => li.textContent
        .replace(/\\s+/g, ' ').trim()),
    dry: !$('confirm-dry').hidden,
    hands: [...document.querySelectorAll('#screen-confirm .hand')]
      .filter((e) => e.offsetParent).length,
  };
})()"""


def _record_want(card: dict[str, Any], margin_type: str) -> tuple[str, list[list[str]]]:
    """Подзаголовок и строки записи из карточки сервера — только разряды и подписи."""
    g = _grouped
    c, lv = card["calc"], card["levels"]
    margin = {"ISOLATED": "изол. ", "CROSSED": "кросс "}.get(margin_type, "")
    price = g(lv["limit_price"]) if lv["entry_type"] == "LIMIT" else f"≈ {g(c['entry_price'])}"
    kind = {"MARKET": "маркет", "LIMIT": "лимит"}[lv["entry_type"]]
    sub = f"{kind} {price} · {margin}{lv['leverage']}×"
    stop_tail = "" if c["distance_percent"] is None else f" · {c['distance_percent']}%"
    rows = [
        ["объём", f"{g(c['quantity'])} XRP"],
        ["стоп", f"{g(lv['stop_loss'])}{stop_tail}"],
        ["тейк", "нет" if lv["take_profit"] is None else g(lv["take_profit"])],
        ["риск с комиссией", f"{g(c['risk_usd'])} $ · {c['risk_percent']}%"],
        ["RR", "—" if c["rr"] is None else f"1 : {c['rr']}"],
        ["ликвидация", "—" if c["liq_estimate"] is None else f"≈ {g(c['liq_estimate'])}"],
        ["комиссия", f"≈ {g(c['fee_usd'])} $"],
    ]
    return sub, rows


@scenario("design_confirm", "Д.5: «ЗАПИСЬ № opening_id», строки = карточка сервера, «было → стало» "
                            "из превью и карточки (только разряды), таймер «действует M:SS»")
async def design_confirm(page: Page, url: str) -> None:
    tap, card_tap = PreviewTap(), CardTap()
    await open_page(page, url, Setup(native=True, rules=[
        Rule("/api/open/preview", "patch", patch=tap, method="POST"),
        Rule("/api/open", "patch", patch=card_tap, method="POST", exact=True)]))
    me = await page.js("fetch('/api/me').then((r) => r.json())")
    await _ready_form(page, True)
    await _to_confirm(page, True)
    await page.pump(0.3)
    got = await page.js(RECORD_JS)
    res = card_tap.last
    page.expect(got["number"] == f"ЗАПИСЬ № {res['opening_id']}",
                f"номер записи {got['number']!r}, opening_id {res['opening_id']}")
    sub, rows = _record_want(res["card"], me["margin_type"])
    page.expect(got["sub"] == sub, f"подзаголовок {got['sub']!r} ≠ {sub!r}")
    page.expect(got["rows"] == rows, f"строки записи {got['rows']} ≠ сервер {rows}")
    page.expect(got["diff"] == ["объём: 86 482 → 86 510"], f"было → стало: {got['diff']}")
    page.expect(got["hands"] == 1, f"пометок «от руки» {got['hands']}, ждали 1 (пересчёт)")
    page.expect(bool(re.fullmatch(r"действует \d+:\d\d", got["timer"] or "")),
                f"таймер {got['timer']!r}")
    page.expect(got["dry"] == bool(me["flags"]["dry_run"]), f"строка сухого прогона {got['dry']}")


def _refused(data: dict[str, Any]) -> None:
    data["opening_id"] = None
    data["status"] = "REFUSED"
    data["expires_at"] = None
    data["card"]["can_open"] = False


@scenario("design_confirm_refused", "Д.5: отказ (opening_id нет) — «ЗАПИСЬ» без номера, кнопки нет")
async def design_confirm_refused(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True, rules=[
        Rule("/api/open", "patch", patch=CardTap(_refused), method="POST", exact=True)]))
    await _ready_form(page, True)
    s = await _to_confirm(page, True)
    got = await page.js(RECORD_JS)
    page.expect(got["number"] == "ЗАПИСЬ", f"номер без opening_id: {got['number']!r}")
    page.expect(s.get("main_button") is None, f"кнопка при отказе: {s.get('main_button')!r}")
    page.expect(got["timer"] is None, f"таймер при отказе: {got['timer']!r}")


@scenario("confirm_double", "Д.5: двойное нажатие «Подтвердить» (8.0, showConfirm) — один вопрос, "
                            "один /confirm, одна сделка")
async def confirm_double(page: Page, url: str) -> None:
    await open_page(page, url, Setup(native=True))
    await page.js(FAKE_SHOW_CONFIRM)
    await _ready_form(page, True)
    await _to_confirm(page, True)
    await page.js("Telegram.WebView.receiveEvent('main_button_pressed');"
                  " Telegram.WebView.receiveEvent('main_button_pressed');")
    await page.wait_for(_done, 25, "итог «Сделка #N»")
    await page.pump(1)
    asked = await page.js("window.__asked")
    page.expect(len(asked) == 1, f"showConfirm спрошен {len(asked)} раз")
    page.expect(len(_confirms(page)) == 1, f"/confirm: {len(_confirms(page))}, ждали 1")


@scenario("confirm_double_old", "Д.5: старый клиент (6.0) — второе нажатие через ≥ 400 мс "
                                "подтверждает, третье сразу за ним — ничего: один /confirm")
async def confirm_double_old(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    await _ready_form(page, False)
    await _to_confirm(page, False)
    # Осознанное второе нажатие — через SECOND_PRESS (≥ 400 мс); третье сразу за ним — уже в
    # отправке и ничего не шлёт.
    await page.click("#confirm-action")
    await page.pump(SECOND_PRESS)
    await page.js("const b = document.getElementById('confirm-action'); b.click();"
                  " setTimeout(() => b.click(), 30);")
    await page.wait_for(_done, 25, "итог «Сделка #N»")
    page.expect(len(_confirms(page)) == 1, f"/confirm: {len(_confirms(page))}, ждали 1")


@scenario("confirm_double_burst", "Д.5: старый клиент (6.0) — залп из трёх нажатий быстрее 400 мс "
                                  "— 0 /confirm, экран на «Нажми ещё раз», без ошибок")
async def confirm_double_burst(page: Page, url: str) -> None:
    await open_page(page, url, Setup())
    await _ready_form(page, False)
    await _to_confirm(page, False)
    await page.js("const b = document.getElementById('confirm-action'); b.click(); b.click();"
                  " setTimeout(() => b.click(), 100);")
    await page.pump(2)
    s = await page.state()
    page.expect(not _confirms(page), f"/confirm: {len(_confirms(page))}, ждали 0 (залп)")
    page.expect("Нажми ещё раз" in (s.get("confirm_status") or ""),
                f"экран после залпа: {s.get('confirm_status')!r}")
    page.expect(s.get("confirm_action") == "Подтвердить" and not _done(s),
                f"кнопка после залпа: {s.get('confirm_action')!r}")



# Д.6 — экран позиций: полоска, PnL, журнал — числа и доли сервера, фронт только ставит

class PositionsTap:
    """Ответы /api/positions (правило «patch»): у LONG — PnL «-1.23 $» (знак -1) и цена за
    тейком (mark_at 1, край take), у SHORT — PnL «0.00 $» (знак 0). Ответ сохраняется —
    эталон: страница обязана показать ровно это."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    def __call__(self, data: Any) -> Any:
        if isinstance(data, dict):
            for p in data.get("positions") or []:
                if p["side"] == "LONG":
                    p["pnl_text"], p["pnl_sign"] = "-1.23 $", -1
                    if p.get("bar"):
                        p["bar"]["mark_at"], p["bar"]["mark_edge"] = "1.0000", "take"
                else:
                    p["pnl_text"], p["pnl_sign"] = "0.00 $", 0
            self.seen.append(data)
        return data

    @property
    def last(self) -> dict[str, Any]:
        if not self.seen:
            raise ScenarioError("ответ /api/positions не пойман")
        return self.seen[-1]


POSITIONS_DESIGN_JS = """(() => {
  const root = getComputedStyle(document.documentElement);
  const cards = [...document.querySelectorAll('#positions-list .position-card')].map((c) => {
    const pnl = c.querySelector('.position-pnl');
    const track = c.querySelector('.position-bar-track');
    const mark = c.querySelector('.position-bar-mark');
    let bar = null;
    if (track && mark) {
      const t = track.getBoundingClientRect();
      const m = mark.getBoundingClientRect();
      bar = {
        mark_at: track.dataset.markAt, entry_at: track.dataset.entryAt,
        mark_offset: (m.left + m.width / 2 - t.left) / t.width,
        mark_class: mark.className,
        labels: [...c.querySelectorAll('.position-bar-labels span')].map((s) => s.textContent),
      };
    }
    return {
      id: c.dataset.position, danger: c.classList.contains('danger'),
      border: getComputedStyle(c).borderTopColor,
      name: c.querySelector('.position-symbol').textContent,
      pnl: pnl.textContent, pnl_class: pnl.className, pnl_color: getComputedStyle(pnl).color,
      bar, chat: c.querySelector('[data-chat]') ? c.querySelector('[data-chat]').textContent : null,
    };
  });
  return {
    cards,
    journal: [...document.querySelectorAll('#positions-journal-list li')]
      .map((li) => li.textContent),
    red: root.getPropertyValue('--red').trim(),
    red_text: root.getPropertyValue('--red-text').trim(),
    muted: root.getPropertyValue('--muted').trim(),
    scroll_x: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
})()"""


@scenario("design_positions", "Д.6: позиции — PnL текстом и цветом по знаку сервера, полоска по "
                              "доле сервера (за краем — край), без стопа — красная рамка и без "
                              "полоски, журнал без хвоста нулей, «Действия →», 320 px")
async def design_positions(page: Page, url: str) -> None:
    tap = PositionsTap()
    await open_page(page, url, Setup(preset="hedge", rules=[
        Rule("/api/positions", "patch", patch=tap),
        Rule("/api/me", "patch", patch=_with_bot)]))
    await _to_positions(page)
    await page.pump(0.5)
    got = await page.js(POSITIONS_DESIGN_JS)
    server = {f"{p['symbol']}:{p['side']}": p for p in tap.last["positions"]}
    cards = {c["id"]: c for c in got["cards"]}
    page.expect(set(cards) == set(server), f"карточки {sorted(cards)} ≠ сервер {sorted(server)}")
    long, short = cards.get("XRP-USDT:LONG"), cards.get("XRP-USDT:SHORT")
    if not long or not short:
        raise ScenarioError(f"нет карточек хеджа: {sorted(cards)}")
    page.expect(long["name"] == "XRP · Лонг" and short["name"] == "XRP · Шорт",
                f"заголовки: {long['name']!r}, {short['name']!r}")
    page.expect(long["pnl"] == "-1.23 $" and "down" in long["pnl_class"]
                and long["pnl_color"] == _rgb(got["red_text"]), f"PnL лонга: {long}")
    page.expect(short["pnl"] == "0.00 $" and "flat" in short["pnl_class"]
                and short["pnl_color"] == _rgb(got["muted"]), f"PnL шорта: {short}")
    sbar = server["XRP-USDT:LONG"]["bar"]
    bar = long["bar"]
    if bar is None or sbar is None:
        raise ScenarioError(f"полоски лонга нет: страница {bar}, сервер {sbar}")
    page.expect(bar["labels"] == [sbar["stop"], sbar["entry"], sbar["take"]],
                f"подписи полоски {bar['labels']}")
    page.expect(bar["mark_at"] == sbar["mark_at"] and abs(bar["mark_offset"] - 1.0) < 0.02
                and "edge-take" in bar["mark_class"], f"отметка за тейком: {bar}")
    page.expect(not long["danger"], "у лонга со стопом красная рамка")
    page.expect(short["danger"] and short["border"] == _rgb(got["red"]) and short["bar"] is None,
                f"шорт без стопа: рамка {short['border']}, полоска {short['bar']}")
    page.expect(long["chat"] == "Действия →", f"кнопка действий: {long['chat']!r}")
    page.expect(got["journal"] and not any(re.search(r"\\.\\d*0000\\b", j) for j in got["journal"]),
                f"журнал с хвостом нулей: {got['journal']}")

    await page.viewport(320, 844)
    await page.pump(1)
    got = await page.js(POSITIONS_DESIGN_JS)
    page.expect(got["scroll_x"] == 0, f"позиции 320 px: прокрутка {got['scroll_x']}")


@scenario("design_positions_bar", "Д.6: отметка цены — ровно на доле сервера (без подмены)")
async def design_positions_bar(page: Page, url: str) -> None:
    seen: list[dict[str, Any]] = []

    def keep(data: Any) -> Any:
        if isinstance(data, dict):
            seen.append(data)
        return data

    await open_page(page, url, Setup(preset="hedge", rules=[
        Rule("/api/positions", "patch", patch=keep)]))
    await _to_positions(page)
    await page.pump(0.5)
    got = await page.js(POSITIONS_DESIGN_JS)
    if not seen:
        raise ScenarioError("ответ /api/positions не пойман")
    server = {f"{p['symbol']}:{p['side']}": p for p in seen[-1]["positions"]}
    long = next((c for c in got["cards"] if c["id"] == "XRP-USDT:LONG"), None)
    sbar = server.get("XRP-USDT:LONG", {}).get("bar")
    if long is None or long["bar"] is None or sbar is None:
        raise ScenarioError(f"полоски лонга нет: {long}, сервер {sbar}")
    at = float(sbar["mark_at"])
    page.note(f"доля сервера {sbar['mark_at']}, на странице {long['bar']['mark_offset']:.4f}")
    page.expect(abs(long["bar"]["mark_offset"] - at) < 0.02 and sbar["mark_edge"] is None,
                f"отметка не на доле сервера: {long['bar']} vs {sbar}")


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
