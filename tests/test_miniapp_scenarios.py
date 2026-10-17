"""Сценарии браузера Mini App (`scripts/miniapp_scenarios.py`, `miniapp_browser_check
--scenario`) — без Chrome, на фейковом транспорте DevTools: ответы по своему id,
перехват сети по правилам (свой статус, правка ответа, обрыв, зависание, число раз),
журнал запросов к /api, замечания без следов своих правил, реестр и коды выхода.
Сами сценарии проверяются в Chrome на стенде (handoff)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import miniapp_browser_check as bc
from scripts import miniapp_scenarios as ms

URL = "http://127.0.0.1:8765/"


class FakeBrowser:
    """Отвечает на команды по id; события — в очереди до ответов. evaluate — из
    `evaluate(expr)` (исключение → exceptionDetails), тело ответа — `server_body`."""

    def __init__(self, evaluate: Callable[[str], Any] | None = None,
                 server_body: Any = None, *, silent: tuple[str, ...] = ()) -> None:
        self.sent: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.evaluate = evaluate or (lambda expr: None)
        self.server_body = server_body
        self.silent = silent

    def push(self, *events: dict[str, Any]) -> None:
        for event in events:
            self.queue.put_nowait(event)

    async def send(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)
        method = msg["method"]
        if method in self.silent:
            return
        if method == "Runtime.evaluate":
            try:
                value = self.evaluate(msg["params"]["expression"])
                result: dict[str, Any] = {"result": {"type": "object", "value": value}}
            except RuntimeError as exc:
                result = {"result": {}, "exceptionDetails": {
                    "text": "Uncaught", "exception": {"description": str(exc)}}}
            self.push({"id": msg["id"], "result": result})
        elif method == "Fetch.getResponseBody":
            body = base64.b64encode(json.dumps(self.server_body).encode()).decode()
            self.push({"id": msg["id"], "result": {"body": body, "base64Encoded": True}})
        else:
            self.push({"id": msg["id"], "result": {}})

    async def receive(self) -> dict[str, Any]:
        return await self.queue.get()

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent]

    def last(self, method: str) -> dict[str, Any]:
        return next(m for m in reversed(self.sent) if m["method"] == method)["params"]


def _paused(url: str, *, method: str = "GET", rid: str = "R1", **extra: Any) -> dict[str, Any]:
    return {"method": "Fetch.requestPaused",
            "params": {"requestId": rid, "request": {"url": url, "method": method}, **extra}}


def _sent_request(url: str, *, method: str = "GET", body: Any = None,
                  rid: str = "N1") -> dict[str, Any]:
    request: dict[str, Any] = {"url": url, "method": method}
    if body is not None:
        request["postData"] = json.dumps(body)
    return {"method": "Network.requestWillBeSent", "params": {"requestId": rid,
                                                               "request": request}}


def _decoded(params: dict[str, Any]) -> Any:
    return json.loads(base64.b64decode(params["body"]))


# --- протокол ------------------------------------------------------------------------


async def test_call_waits_for_own_reply_through_events() -> None:
    t = FakeBrowser()
    page = ms.Page(t)
    t.push({"method": "Runtime.consoleAPICalled", "params": {"type": "log", "args": []}},
           {"id": 99, "result": {"x": 1}})                     # чужой ответ — не наш
    assert await page.call("Page.enable") == {}
    assert page.collector.console == [("log", "")]
    assert 99 in page.collector.responses                     # не перепутан с нашим


async def test_call_without_reply_times_out() -> None:
    page = ms.Page(FakeBrowser(silent=("Page.navigate",)))
    with pytest.raises(ms.ScenarioError, match="нет ответа"):
        await page.call("Page.navigate", {"url": URL}, timeout=0.05)


async def test_error_reply_and_js_exception_stop_scenario() -> None:
    t = FakeBrowser(evaluate=lambda expr: (_ for _ in ()).throw(RuntimeError("TypeError: x")))
    page = ms.Page(t)
    with pytest.raises(ms.ScenarioError, match="TypeError: x"):
        await page.js("x.y")
    t.push({"id": 2, "error": {"message": "No node"}})
    t.silent = ("DOM.focus",)
    with pytest.raises(ms.ScenarioError, match="No node"):
        await page.call("DOM.focus")


async def test_api_requests_are_logged_with_body_and_query() -> None:
    t = FakeBrowser()
    page = ms.Page(t)
    t.push(_sent_request(URL + "api/market/XRP-USDT/candles?interval=1h", rid="a"),
           _sent_request(URL + "api/open/preview", method="POST", body={"leverage": 10}, rid="b"),
           _sent_request(URL + "app.js", rid="c"))
    await page.call("Page.enable")
    assert [(r.method, r.path) for r in page.requests] == [
        ("GET", "/api/market/XRP-USDT/candles?interval=1h"), ("POST", "/api/open/preview")]
    assert page.requests[0].query == {"interval": ["1h"]}
    assert page.api("/api/open/preview", method="POST")[0].body == {"leverage": 10}
    assert page.api("/api/open/preview") == []                # GET — другой метод
    assert page.api("/api/market/", since=10**9) == []


# --- перехват сети --------------------------------------------------------------------


async def _intercept(rule: ms.Rule | None, *events: dict[str, Any],
                     server_body: Any = None) -> tuple[FakeBrowser, ms.Page]:
    t = FakeBrowser(server_body=server_body)
    page = ms.Page(t)
    if rule is not None:
        page.intercept(rule)
    t.push(*events)
    await page.call("Page.enable")
    return t, page


async def test_without_rule_request_continues() -> None:
    t, _ = await _intercept(None, _paused(URL + "api/me"))
    assert t.last("Fetch.continueRequest") == {"requestId": "R1"}


async def test_status_rule_answers_without_server() -> None:
    body = {"code": "EXCHANGE_UNAVAILABLE", "message": "Биржа не ответила"}
    t, page = await _intercept(ms.Rule("/api/market/", "status", status=503, body=body),
                               _paused(URL + "api/market/XRP-USDT?interval=5m"))
    sent = t.last("Fetch.fulfillRequest")
    assert sent["responseCode"] == 503 and _decoded(sent) == body
    assert "Fetch.continueRequest" not in t.methods()
    assert page.rules[0].hits == 1


async def test_fail_rule_breaks_before_server_and_hang_never_answers() -> None:
    t, _ = await _intercept(ms.Rule("telegram.org", "fail"),
                            _paused("https://telegram.org/js/telegram-web-app.js"))
    assert t.last("Fetch.failRequest") == {"requestId": "R1", "errorReason": "InternetDisconnected"}
    t, page = await _intercept(ms.Rule("telegram.org", "hang"),
                               _paused("https://telegram.org/js/telegram-web-app.js"))
    assert not any(m.startswith("Fetch.") for m in t.methods())
    assert page.hung == ["https://telegram.org/js/telegram-web-app.js"]


async def test_patch_rule_edits_server_response() -> None:
    def patch(data: Any) -> Any:
        return {**data, "leverage": None}

    t, page = await _intercept(
        ms.Rule("/api/market/", "patch", patch=patch),
        _paused(URL + "api/market/XRP-USDT?interval=5m"),
        _paused(URL + "api/market/XRP-USDT?interval=5m", responseStatusCode=200,
                responseHeaders=[{"name": "Content-Type", "value": "application/json"},
                                 {"name": "Content-Length", "value": "42"}]),
        server_body={"last": "1.4950", "leverage": {"max_long": 125}})
    assert t.methods().count("Fetch.continueRequest") == 1
    assert t.last("Fetch.continueRequest") == {"requestId": "R1", "interceptResponse": True}
    sent = t.last("Fetch.fulfillRequest")
    assert _decoded(sent) == {"last": "1.4950", "leverage": None}
    assert sent["responseCode"] == 200
    assert [h["name"] for h in sent["responseHeaders"]] == ["Content-Type"]
    assert page.rules[0].hits == 1


async def test_rule_times_and_method() -> None:
    rule = ms.Rule("/api/open/", "fail", method="POST", times=1)
    t, _ = await _intercept(rule,
                            _paused(URL + "api/open/7", rid="A"),                    # GET
                            _paused(URL + "api/open/7/confirm", method="POST", rid="B"),
                            _paused(URL + "api/open/7/confirm", method="POST", rid="C"))
    calls = [(m["method"], m["params"]["requestId"]) for m in t.sent
             if m["method"].startswith("Fetch.")]
    assert calls == [("Fetch.continueRequest", "A"), ("Fetch.failRequest", "B"),
                     ("Fetch.continueRequest", "C")]
    assert rule.hits == 1


# --- замечания ------------------------------------------------------------------------


def _with(page: ms.Page, *events: dict[str, Any]) -> ms.Page:
    for e in events:
        page.collector.feed(e)
    return page


def _status(code: int, url: str) -> dict[str, Any]:
    return {"method": "Network.responseReceived",
            "params": {"response": {"status": code, "url": url}}}


def test_own_rule_traces_are_not_issues_but_others_are() -> None:
    page = ms.Page(FakeBrowser())
    page.intercept(ms.Rule("/api/market/", "status", status=503, hits=1))
    page.intercept(ms.Rule("/api/me", "status", status=500))             # не сработало
    _with(page,
          _status(503, URL + "api/market/XRP-USDT?interval=5m"),
          {"method": "Log.entryAdded", "params": {"entry": {
              "level": "error", "source": "network", "text": "Failed to load resource",
              "url": URL + "api/market/XRP-USDT?interval=5m"}}},
          _status(500, URL + "api/me"),
          _status(404, URL + "favicon.ico"),
          {"method": "Runtime.consoleAPICalled", "params": {"type": "error", "args": [
              {"value": "boom /api/market/"}]}})
    page.expect(False, "ожидание")
    issues = page.issues()
    assert issues[0] == "ожидание"
    assert "HTTP 500: http://127.0.0.1:8765/api/me" in issues
    assert "HTTP 404: http://127.0.0.1:8765/favicon.ico" in issues
    assert any(i.startswith("console.error: boom") for i in issues)    # console — всегда
    assert not any("503" in i or "log.error" in i for i in issues)


def test_failed_load_names_its_url() -> None:
    c = bc.Collector()
    c.feed(_sent_request("https://telegram.org/js/telegram-web-app.js", rid="S"))
    c.feed({"method": "Network.loadingFailed",
            "params": {"requestId": "S", "errorText": "net::ERR_INTERNET_DISCONNECTED"}})
    assert c.issues() == ["загрузка не удалась: net::ERR_INTERNET_DISCONNECTED "
                          "https://telegram.org/js/telegram-web-app.js"]


async def test_wait_for_returns_state_or_records_what_was_seen() -> None:
    states = iter([{"screens": ["screen-loading"]}, {"screens": ["screen-market"], "last": "1"}])
    page = ms.Page(FakeBrowser(evaluate=lambda expr: next(states)))
    s = await page.wait_for(lambda s: "screen-market" in s["screens"], 2, "рынок")
    assert s["last"] == "1" and page.problems == []

    page = ms.Page(FakeBrowser(evaluate=lambda expr: {"screens": ["screen-error"],
                                                      "error_title": "Сессия истекла"}))
    await page.wait_for(lambda s: "screen-market" in s["screens"], 0.3, "рынок")
    assert len(page.problems) == 1
    assert page.problems[0].startswith("не дождался за 0.3 с: рынок [")
    assert "error_title='Сессия истекла'" in page.problems[0]


async def test_wait_for_survives_page_reload() -> None:
    def evaluate(expr: str) -> Any:
        raise RuntimeError("Execution context was destroyed")

    page = ms.Page(FakeBrowser(evaluate=evaluate))
    assert await page.wait_for(lambda s: True, 0.3, "страница") == {}
    assert "Execution context was destroyed" in page.problems[0]


# --- открытие страницы ----------------------------------------------------------------


async def test_open_page_prepares_everything_before_navigate() -> None:
    t = FakeBrowser()
    page = ms.Page(t)
    rule = ms.Rule("telegram.org", "fail")
    await ms.open_page(page, URL, ms.Setup(native=True, storage={"interval": "1m"},
                                           rules=[rule], wait_screen=None))
    methods = t.methods()
    nav = methods.index("Page.navigate")
    for before in ("Network.setCacheDisabled", "Emulation.setDeviceMetricsOverride",
                   "Fetch.enable"):
        assert methods.index(before) < nav, before
    scripts = [m["params"]["source"] for m in t.sent
               if m["method"] == "Page.addScriptToEvaluateOnNewDocument"]
    assert "securitypolicyviolation" in scripts[0]
    assert '"ma:anon:symbol", "\\"XRP-USDT\\""' in scripts[1]
    assert '"ma:anon:interval", "\\"1m\\""' in scripts[1]
    assert t.last("Page.navigate") == {"url": URL + "#tgWebAppVersion=8.0"}
    assert page.rules == [rule]
    patterns = [p["urlPattern"] for p in t.last("Fetch.enable")["patterns"]]
    assert patterns == ["*/api/*", "*telegram.org*"]


def test_storage_script_matches_miniapp_keys() -> None:
    assert ms.STORAGE + "symbol" == bc.STORAGE_SYMBOL_KEY
    script = ms.storage_script({"chartType": "line"})
    assert script == ('try { localStorage.setItem("ma:anon:chartType", "\\"line\\""); } '
                      "catch (e) {}")


# --- реестр и запуск ------------------------------------------------------------------


def test_registry_covers_queue_scenarios() -> None:
    """п.5 очереди handoff: рынок 2.3, свечи 2.3а, SDK 2а, форма 2.4; подтверждение 2.5."""
    assert set(ms.SCENARIOS) == {
        "search", "poll", "timeframe", "theme", "widths", "hidden", "leave_return",
        "empty_candles", "load_503", "poll_503", "expired_401", "candles_1m",
        "sdk_block", "sdk_hang",
        "form", "form_native", "form_plan10", "form_planfree", "form_nokey", "form_leverr",
        "confirm_market", "confirm_native", "confirm_live", "confirm_retry", "confirm_warn",
        "confirm_limit", "confirm_expired", "confirm_block", "confirm_back",
        "pos_empty", "pos_list", "pos_unlinked", "pos_chat", "pos_no_key", "pos_503",
        "pos_401", "pos_poll", "pos_to_market", "menu_native",
    }
    assert [s.name for s in ms.select("all")] == list(ms.SCENARIOS)
    assert ms.select("poll") == [ms.SCENARIOS["poll"]]
    with pytest.raises(bc.EnvironmentNotReadyError, match="нет сценария"):
        ms.select("nope")


async def test_run_one_turns_errors_into_issues() -> None:
    async def broken(page: ms.Page, url: str) -> None:
        raise ms.ScenarioError("Runtime.evaluate: нет ответа за 10 с")

    item = ms.Scenario("x", "проба", broken)
    page = await ms.run_one(FakeBrowser(), URL, item)
    assert page.issues() == ["сценарий прерван: Runtime.evaluate: нет ответа за 10 с"]
    assert ms.render(item, page).startswith("✗ x — проба")


@pytest.fixture
def chrome_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "chrome-stub"
    exe.write_bytes(b"")
    return exe


def _page(*problems: str) -> ms.Page:
    page = ms.Page(FakeBrowser())
    page.problems.extend(problems)
    return page


@pytest.mark.parametrize(("problems", "code"), [((), 0), (("плохо",), 1)])
def test_main_scenario_exit_codes(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                                  capsys: pytest.CaptureFixture[str],
                                  problems: tuple[str, ...], code: int) -> None:
    seen: list[list[str]] = []

    async def fake(url: str, chrome: Path, items: list[ms.Scenario]) -> list[Any]:
        seen.append([i.name for i in items])
        return [(item, _page(*problems)) for item in items]

    monkeypatch.setattr(bc, "check_scenarios", fake)
    monkeypatch.setattr(bc, "check", None)                    # обычная проверка не идёт
    assert bc.main(["--chrome", str(chrome_exe), "--scenario", "poll"]) == code
    assert seen == [["poll"]]
    out = capsys.readouterr().out
    assert f"сценариев 1, с замечаниями {len(problems)}" in out


def test_main_unknown_scenario_is_exit_2(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(bc, "check_scenarios", None)
    assert bc.main(["--chrome", str(chrome_exe), "--scenario", "nope"]) == 2
    assert "нет сценария" in capsys.readouterr().err


def test_scenarios_not_in_docker_image() -> None:
    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".dockerignore").read_text(encoding="utf-8").split()
    assert "scripts/miniapp_scenarios.py" in ignored


async def test_exact_rule_matches_path_not_prefix() -> None:
    rule = ms.Rule("/api/open", "fail", method="POST", exact=True)
    t, _ = await _intercept(rule,
                            _paused(URL + "api/open/preview", method="POST", rid="P"),
                            _paused(URL + "api/open", method="POST", rid="O"))
    calls = [(m["method"], m["params"]["requestId"]) for m in t.sent
             if m["method"].startswith("Fetch.")]
    assert calls == [("Fetch.continueRequest", "P"), ("Fetch.failRequest", "O")]


async def test_scenario_crash_is_an_issue_not_a_traceback() -> None:
    async def crash(page: ms.Page, url: str) -> None:
        raise KeyError("card")

    page = await ms.run_one(FakeBrowser(), URL, ms.Scenario("x", "проба", crash))
    assert page.issues() == ["сценарий упал: KeyError: 'card'"]


async def test_stand_is_reset_before_page_opens() -> None:
    t = FakeBrowser()
    page = ms.Page(t)
    order: list[str] = []

    async def reset() -> None:
        order.append(f"reset после {len(t.sent)} команд")

    page.reset_stand = reset
    await ms.open_page(page, URL, ms.Setup(wait_screen=None))
    assert order == ["reset после 0 команд"]


async def test_stand_positions_seeded_after_reset_before_page() -> None:
    """Setup.preset (П.1): позиции стенда — после сброса и до первой команды браузера;
    без preset — не трогаются."""
    t = FakeBrowser()
    page = ms.Page(t)
    order: list[str] = []

    async def reset() -> None:
        order.append("reset")

    async def seed(preset: str) -> None:
        order.append(f"seed {preset} после {len(t.sent)} команд")

    page.reset_stand, page.seed_stand = reset, seed
    await ms.open_page(page, URL, ms.Setup(wait_screen=None, preset="hedge"))
    assert order == ["reset", "seed hedge после 0 команд"]
    order.clear()
    await ms.open_page(page, URL, ms.Setup(wait_screen=None))
    assert order == ["reset"]


def test_presets_of_scenarios_exist_on_stand() -> None:
    import re

    from scripts import miniapp_dev

    text = Path(ms.__file__).read_text(encoding="utf-8")
    used = set(re.findall(r'preset="(\w+)"', text))
    assert used and used <= set(miniapp_dev.PRESETS)


async def test_sdk_failure_on_open_stops_scenario_as_network() -> None:
    page = ms.Page(FakeBrowser(evaluate=lambda expr: {"screens": ["screen-sdk"]}))
    with pytest.raises(ms.ScenarioError, match=r"telegram\.org не загрузился"):
        await ms.open_page(page, URL, ms.Setup())


def test_module_constants_assigned_once() -> None:
    """17.10 (П.1): вторая NO_KEY ниже по файлу перезаписала строку сценариев формы 2.4 —
    form_nokey упал. Имя верхнего уровня присваивается в модуле один раз."""
    import ast
    from collections import Counter

    tree = ast.parse(Path(ms.__file__).read_text(encoding="utf-8"))
    names = Counter(
        t.id for node in tree.body if isinstance(node, ast.Assign | ast.AnnAssign)
        for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(t, ast.Name)
    )
    assert [n for n, c in names.items() if c > 1] == []
