"""`scripts/miniapp_browser_check.py` без настоящего Chrome: разбор событий
DevTools, порядок команд, ответ evaluate только по своему id, коды выхода."""

from __future__ import annotations

import asyncio
import base64
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import miniapp_browser_check as bc

_ROOT = Path(__file__).resolve().parents[1]


def _console(kind: str, *values: str) -> dict[str, Any]:
    return {"method": "Runtime.consoleAPICalled",
            "params": {"type": kind, "args": [{"value": v} for v in values]}}


def _response(status: int, url: str) -> dict[str, Any]:
    return {"method": "Network.responseReceived",
            "params": {"response": {"status": status, "url": url}}}


def _loaded(**extra: Any) -> dict[str, Any]:
    """Состояние догруженной страницы (как отдаёт STATE_EXPR)."""
    return {"title": "Открыть сделку", "visible_screens": ["screen-market"], "charts": 0,
            "lightweight_charts": "5.2.1", "telegram_sdk": True, "csp_listener": True,
            "seed": None, **extra}


# --- только 127.0.0.1, путь к Chrome --------------------------------------------


@pytest.mark.parametrize("url", [
    "http://localhost:8765/", "http://0.0.0.0:8765/", "https://example.com/",
    "http://127.0.0.1.example.com/", "file:///C:/x.html",
])
def test_refuses_non_localhost_url(url: str) -> None:
    with pytest.raises(bc.EnvironmentNotReadyError, match=r"127\.0\.0\.1"):
        bc.require_localhost(url)


def test_accepts_stand_and_devtools_urls() -> None:
    bc.require_localhost("http://127.0.0.1:8765/")
    bc.require_localhost("ws://127.0.0.1:51234/devtools/page/ABC")


def test_chrome_command_listens_only_on_localhost(tmp_path: Path) -> None:
    cmd = bc.chrome_command(tmp_path / "chrome.exe", tmp_path)
    assert "--remote-debugging-address=127.0.0.1" in cmd
    assert f"--user-data-dir={tmp_path}" in cmd
    assert "--headless=new" in cmd


def test_chrome_path_from_arg_or_env(tmp_path: Path) -> None:
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    assert bc.chrome_path(str(exe), env={}) == exe
    assert bc.chrome_path(None, env={bc.CHROME_ENV: str(exe)}) == exe
    with pytest.raises(bc.EnvironmentNotReadyError, match="не задан"):
        bc.chrome_path(None, env={})
    with pytest.raises(bc.EnvironmentNotReadyError, match="не найден"):
        bc.chrome_path(str(tmp_path / "нет.exe"), env={})


def test_no_hardcoded_chrome_path() -> None:
    text = (_ROOT / "scripts" / "miniapp_browser_check.py").read_text(encoding="utf-8")
    assert "chrome.exe" not in text.lower()
    assert "Program Files" not in text


def test_fresh_profile_is_removed() -> None:
    with bc.fresh_profile() as profile:
        (profile / "Default").mkdir()
        (profile / "Default" / "Cache").write_text("x", encoding="utf-8")
        assert profile.is_dir()
    assert not profile.exists()


def test_not_in_docker_image() -> None:
    lines = (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "scripts/miniapp_browser_check.py" in lines


# --- разбор событий ------------------------------------------------------------------


def test_clean_page_has_no_issues() -> None:
    c = bc.Collector()
    for msg in (_console("log", "[Telegram.WebView] > postEvent web_app_ready"),
                _response(200, "http://127.0.0.1:8765/app.js"),
                {"method": "Log.entryAdded", "params": {"entry": {"level": "info", "text": "ok"}}}):
        c.feed(msg)
    assert c.issues() == []


@pytest.mark.parametrize(("msg", "fragment"), [
    (_console("error", "boom"), "console.error: boom"),
    (_console("warning", "BackButton is not supported"), "console.warning"),
    (_console("error", bc.CSP_MARK, "style-src-elem", "inline", "app.js:3"), "CSP: CSP-VIOLATION"),
    ({"method": "Log.entryAdded", "params": {"entry": {
        "level": "error", "source": "security",
        "text": "Refused to apply inline style because it violates the following "
                "Content Security Policy directive"}}}, "CSP: Refused"),
    ({"method": "Log.entryAdded", "params": {"entry": {
        "level": "error", "source": "network", "text": "Failed to load resource"}}},
     "log.error: network"),
    ({"method": "Runtime.exceptionThrown", "params": {"exceptionDetails": {
        "text": "Uncaught", "exception": {"description": "TypeError: x is null"}}}},
     "исключение: TypeError"),
    (_response(404, "http://127.0.0.1:8765/favicon.ico"), "HTTP 404"),
    ({"method": "Network.loadingFailed",
      "params": {"errorText": "net::ERR_BLOCKED_BY_CLIENT", "blockedReason": "csp"}},
     "загрузка не удалась"),
])
def test_each_problem_is_an_issue(msg: dict[str, Any], fragment: str) -> None:
    c = bc.Collector()
    c.feed(msg)
    issues = c.issues()
    assert len(issues) == 1
    assert fragment in issues[0]


def test_cancelled_request_is_not_an_issue() -> None:
    c = bc.Collector()
    c.feed({"method": "Network.loadingFailed", "params": {"errorText": "net::ERR_ABORTED",
                                                            "canceled": True}})
    assert c.issues() == []


def test_event_without_id_is_never_a_response() -> None:
    """Регрессия 15.10: при eval_id=None событие (id нет) принималось за ответ."""
    c = bc.Collector()
    c.feed(_console("log", "x"))
    c.feed({"id": None, "method": "Log.entryAdded", "params": {"entry": {"level": "info"}}})
    assert c.responses == {}
    assert None not in c.responses


# --- сессия на фейковом транспорте ------------------------------------------------


class FakeTransport:
    """Отвечает на команды по id; события страницы приходят ДО ответа на evaluate."""

    def __init__(self, page_events: list[dict[str, Any]], *, answer_eval: bool = True,
                 state: Any = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.page_events = page_events
        self.answer_eval = answer_eval
        self.state = state if state is not None else _loaded()

    async def send(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)
        method = msg["method"]
        if method == "Page.navigate":
            for event in self.page_events:
                self.queue.put_nowait(event)
            self.queue.put_nowait({"id": msg["id"], "result": {"frameId": "F"}})
        elif method == "Runtime.evaluate":
            # Сначала ещё события без id — их нельзя принять за ответ.
            self.queue.put_nowait(_console("log", "late"))
            if self.answer_eval:
                self.queue.put_nowait({"id": msg["id"], "result": {"result": {
                    "type": "object", "value": self.state}}})
        else:
            self.queue.put_nowait({"id": msg["id"], "result": {}})

    async def receive(self) -> dict[str, Any]:
        return await self.queue.get()


async def test_session_collects_events_and_reads_state() -> None:
    t = FakeTransport([_console("log", "ready"), _response(200, "http://127.0.0.1:8765/")])
    report = await bc.run_check(t, "http://127.0.0.1:8765/", 0.05)
    assert report.state == _loaded()
    assert report.issues == []
    assert [k for k, _ in report.collector.console] == ["log", "log"]   # ready + late


async def test_commands_order_listener_before_navigate() -> None:
    t = FakeTransport([])
    await bc.run_check(t, "http://127.0.0.1:8765/", 0.01)
    methods = [m["method"] for m in t.sent]
    assert methods.index("Network.setCacheDisabled") < methods.index("Page.navigate")
    assert methods.index("Page.addScriptToEvaluateOnNewDocument") < methods.index("Page.navigate")
    listener = next(m for m in t.sent if m["method"] == "Page.addScriptToEvaluateOnNewDocument")
    assert "securitypolicyviolation" in listener["params"]["source"]
    assert methods[-1] == "Runtime.evaluate"
    assert len({m["id"] for m in t.sent}) == len(t.sent)


async def test_state_comes_only_from_evaluate_reply() -> None:
    """Регрессия eval_id=None: события до evaluate не дают «состояния»."""
    t = FakeTransport([_console("log", "a"), _console("log", "b")], answer_eval=False)
    report = await bc.run_check(t, "http://127.0.0.1:8765/", 0.01)
    assert report.state is None
    assert report.issues == ["состояние страницы не прочитано (evaluate не ответил)"]


async def test_csp_violation_from_listener_fails_session() -> None:
    t = FakeTransport([_console("error", bc.CSP_MARK, "style-src-elem", "inline", ":0")])
    report = await bc.run_check(t, "http://127.0.0.1:8765/", 0.01)
    assert report.issues and report.issues[0].startswith("CSP:")


# --- коды выхода ----------------------------------------------------------------------


@pytest.fixture
def chrome_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "chrome-stub"
    exe.write_bytes(b"")
    return exe


def _report(*events: dict[str, Any], state: Any = None) -> bc.Report:
    c = bc.Collector()
    for e in events:
        c.feed(e)
    return bc.Report(c, _loaded() if state is None else state)


def test_exit_0_when_clean(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                           capsys: pytest.CaptureFixture[str]) -> None:
    async def fake_check(url: str, chrome: Path, wait: float,
                         symbol: str | None = None, local_sdk: bool = True) -> bc.Report:
        return _report(_response(200, url))

    monkeypatch.setattr(bc, "check", fake_check)
    assert bc.main(["--chrome", str(chrome_exe)]) == 0
    assert "замечаний нет" in capsys.readouterr().out


def test_exit_1_lists_issues(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                             capsys: pytest.CaptureFixture[str]) -> None:
    async def fake_check(url: str, chrome: Path, wait: float,
                         symbol: str | None = None, local_sdk: bool = True) -> bc.Report:
        return _report(_console("error", "boom"), _response(404, url + "x.js"))

    monkeypatch.setattr(bc, "check", fake_check)
    assert bc.main(["--chrome", str(chrome_exe)]) == 1
    out = capsys.readouterr().out
    assert "✗ console.error: boom" in out
    assert "✗ HTTP 404" in out


@pytest.mark.parametrize("argv", [
    ["--url", "http://localhost:8765/"],          # не 127.0.0.1
    [],                                           # Chrome не задан
    ["--symbol", "xrp"],                          # не контракт
    ["--symbol", "X');alert(1);//-USDT"],         # не попадёт в скрипт страницы
])
def test_exit_2_when_environment_not_ready(monkeypatch: pytest.MonkeyPatch, argv: list[str],
                                           chrome_exe: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv(bc.CHROME_ENV, raising=False)
    if argv:
        argv = [*argv, "--chrome", str(chrome_exe)]
    monkeypatch.setattr(bc, "check", None)        # дошли бы до проверки — TypeError
    assert bc.main(argv) == 2
    assert "Проверка не проведена" in capsys.readouterr().err


def test_exit_2_when_stand_does_not_answer(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    started: list[Any] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    # Порт 9 на 127.0.0.1 никто не слушает — стенда нет, Chrome не запускается.
    assert bc.main(["--chrome", str(chrome_exe), "--url", "http://127.0.0.1:9/"]) == 2
    assert "стенд не отвечает" in capsys.readouterr().err
    assert started == []


# --- страница не догрузилась, --symbol ------------------------------------------------


@pytest.mark.parametrize(("state", "fragment"), [
    (_loaded(visible_screens=[]), "ни один экран не показан"),
    (_loaded(telegram_sdk=False), "SDK Telegram не загрузился"),
    (_loaded(lightweight_charts=None), "библиотека графиков не загрузилась"),
    ("ok", "не разобрано"),
])
def test_unloaded_page_is_an_issue(state: Any, fragment: str) -> None:
    """Регрессия 16.10: SDK не успел за --wait, экранов нет — а итог был «чисто»."""
    issues = bc.Report(bc.Collector(), state).issues
    assert len(issues) == 1
    assert fragment in issues[0]


def test_loaded_page_without_symbol_needs_no_chart() -> None:
    assert bc.Report(bc.Collector(), _loaded(charts=0)).issues == []


@pytest.mark.parametrize(("charts", "ok"), [(1, True), (0, False), (2, False)])
def test_symbol_requires_exactly_one_chart(charts: int, ok: bool) -> None:
    issues = bc.Report(bc.Collector(), _loaded(charts=charts), expect_chart=True).issues
    assert (issues == []) is ok
    if not ok:
        assert f"графиков на странице {charts}" in issues[0]


def test_seed_script_matches_miniapp_storage_key() -> None:
    """Ключ — как у storage.js (префикс ma, без Telegram — anon) и market.js (symbol)."""
    storage = (_ROOT / "miniapp" / "storage.js").read_text(encoding="utf-8")
    market = (_ROOT / "miniapp" / "market.js").read_text(encoding="utf-8")
    assert 'const PREFIX = "ma";' in storage
    assert '`${PREFIX}:${telegramId ?? "anon"}:`' in storage
    assert 'storage.load("symbol"' in market
    assert bc.STORAGE_SYMBOL_KEY == "ma:anon:symbol"
    script = bc.seed_script("XRP-USDT")
    assert script == ('try { localStorage.setItem("ma:anon:symbol", "\\"XRP-USDT\\""); '
                      'window.__maSeed = "XRP-USDT"; } catch (e) {}')
    # Метка — только после записи: хранилище недоступно — метки нет.
    assert script.index("setItem(") < script.index("window.__maSeed")


def test_seed_script_refuses_non_symbol() -> None:
    with pytest.raises(bc.EnvironmentNotReadyError, match="XRP-USDT"):
        bc.seed_script('X"); alert(1); ("-USDT')


async def test_symbol_seeds_storage_before_navigate() -> None:
    t = FakeTransport([], state=_loaded(charts=1, seed="XRP-USDT"))
    report = await bc.run_check(t, "http://127.0.0.1:8765/", 0.01, symbol="XRP-USDT")
    methods = [m["method"] for m in t.sent]
    sources = [m["params"]["source"] for m in t.sent
               if m["method"] == "Page.addScriptToEvaluateOnNewDocument"]
    assert any("ma:anon:symbol" in src for src in sources)
    seed = next(i for i, m in enumerate(t.sent)
                if "ma:anon:symbol" in m["params"].get("source", ""))
    assert seed < methods.index("Page.navigate")
    assert report.expect_chart is True
    assert report.issues == []


async def test_without_symbol_storage_is_untouched() -> None:
    t = FakeTransport([])
    report = await bc.run_check(t, "http://127.0.0.1:8765/", 0.01)
    assert not any("localStorage" in m["params"].get("source", "") for m in t.sent)
    assert report.expect_chart is False


def test_main_passes_symbol(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path) -> None:
    seen: list[str | None] = []

    async def fake_check(url: str, chrome: Path, wait: float,
                         symbol: str | None = None, local_sdk: bool = True) -> bc.Report:
        seen.append(symbol)
        return _report()

    monkeypatch.setattr(bc, "check", fake_check)
    assert bc.main(["--chrome", str(chrome_exe), "--symbol", "XRP-USDT"]) == 0
    assert bc.main(["--chrome", str(chrome_exe)]) == 0
    assert seen == ["XRP-USDT", None]


# --- SDK telegram.org: копия стенда по умолчанию (Д.4) ------------------------------------

_SDK_SHA256 = "124bdff7ba3fe86c8ba5143467afec21e020405e74abdb92cfdda6c0ee3b5908"


def test_stand_sdk_copy_is_recorded_and_not_shipped() -> None:
    """Копия SDK (telegram.org, Last-Modified 06.10.2026) — байт в байт; в образ и в
    miniapp/ (прод) не попадает. Обновление копии — новый sha256 здесь."""
    import hashlib

    data = bc.STAND_SDK.read_bytes()
    assert hashlib.sha256(data).hexdigest() == _SDK_SHA256
    text = data.decode("utf-8")
    assert "window.Telegram.WebApp" in text and "setBottomBarColor" in text
    assert bc.STAND_SDK.parent.name == "stand_sdk" and bc.STAND_SDK.parent.parent.name == "scripts"
    assert bc.SDK_URL == "https://telegram.org/js/telegram-web-app.js"
    assert "scripts/stand_sdk/" in (_ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert "scripts/stand_sdk/** -text" in (_ROOT / ".gitattributes").read_text(
        encoding="utf-8").splitlines()
    html = (_ROOT / "miniapp" / "index.html").read_text(encoding="utf-8")
    assert f'src="{bc.SDK_URL}"' in html                 # прод грузит настоящий SDK


async def test_run_check_serves_local_sdk_and_continues_rest() -> None:
    sdk = {"method": "Fetch.requestPaused",
           "params": {"requestId": "S", "request": {"url": bc.SDK_URL, "method": "GET"}}}
    other = {"method": "Fetch.requestPaused",
             "params": {"requestId": "O", "request": {"url": "https://telegram.org/x.js",
                                                      "method": "GET"}}}
    t = FakeTransport([sdk, other])
    await bc.run_check(t, "http://127.0.0.1:8765/", 0.05)
    methods = [m["method"] for m in t.sent]
    assert methods.index("Fetch.enable") < methods.index("Page.navigate")
    fulfill = next(m["params"] for m in t.sent if m["method"] == "Fetch.fulfillRequest")
    assert fulfill["requestId"] == "S" and fulfill["responseCode"] == 200
    assert base64.b64decode(fulfill["body"]) == bc.STAND_SDK.read_bytes()
    assert {"name": "Content-Type", "value": "application/javascript"} in fulfill["responseHeaders"]
    cont = [m["params"] for m in t.sent if m["method"] == "Fetch.continueRequest"]
    assert cont == [{"requestId": "O"}]


async def test_run_check_real_sdk_does_not_intercept() -> None:
    t = FakeTransport([])
    await bc.run_check(t, "http://127.0.0.1:8765/", 0.05, local_sdk=False)
    assert "Fetch.enable" not in [m["method"] for m in t.sent]


def test_main_real_sdk_flag(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path) -> None:
    seen: list[bool] = []

    async def fake_check(url: str, chrome: Path, wait: float, symbol: str | None = None,
                         local_sdk: bool = True) -> bc.Report:
        seen.append(local_sdk)
        return _report()

    monkeypatch.setattr(bc, "check", fake_check)
    assert bc.main(["--chrome", str(chrome_exe)]) == 0
    assert bc.main(["--chrome", str(chrome_exe), "--real-sdk"]) == 0
    assert seen == [True, False]


class _SeqTransport(FakeTransport):
    """evaluate отдаёт состояния по очереди (последнее — дальше всегда)."""

    def __init__(self, states: list[Any]) -> None:
        super().__init__([])
        self.states = states

    async def send(self, msg: dict[str, Any]) -> None:
        if msg["method"] == "Runtime.evaluate":
            self.state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        await super().send(msg)


@pytest.mark.parametrize(("charts", "issue"), [([0, 0, 1], False), ([0], True)])
async def test_symbol_check_rereads_until_chart(monkeypatch: pytest.MonkeyPatch,
                                                charts: list[int], issue: bool) -> None:
    """Д.5: после тяжёлого прогона график иногда появляется позже --wait — дочитываем до
    START_TIMEOUT; не появился и тогда — замечание «графиков 0»."""
    monkeypatch.setattr(bc, "START_TIMEOUT", 0.6)
    monkeypatch.setattr(bc, "CHART_POLL", 0.05)
    t = _SeqTransport([_loaded(charts=n, seed="XRP-USDT") for n in charts])
    report = await bc.run_check(t, "http://127.0.0.1:8765/", 0.01, symbol="XRP-USDT")
    assert any("графиков на странице 0" in i for i in report.issues) is issue
    if not issue:
        assert report.state["charts"] == 1


# --- 5.3а: команды до навигации ждут ответа, сид с меткой (SEED_MISSING) ------------------


class _StrictTransport(FakeTransport):
    """Ответ приходит только при чтении; новая команда до ответа на прошлую (до навигации
    включительно) — нарушение порядка. 25.10: без ожидания navigate обгонял сид."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.unanswered: set[int] = set()
        self.violations: list[str] = []
        self.navigated = False

    async def send(self, msg: dict[str, Any]) -> None:
        if not self.navigated and self.unanswered:
            self.violations.append(msg["method"])
        self.unanswered.add(msg["id"])
        await super().send(msg)

    async def receive(self) -> dict[str, Any]:
        msg = await super().receive()
        if isinstance(msg.get("id"), int):
            self.unanswered.discard(msg["id"])
            sent = next(m for m in self.sent if m["id"] == msg["id"])
            if sent["method"] == "Page.navigate":
                self.navigated = True
        return msg


async def test_setup_commands_wait_for_reply_before_next() -> None:
    t = _StrictTransport([], state=_loaded(charts=1, seed="XRP-USDT"))
    report = await bc.run_check(t, "http://127.0.0.1:8765/", 0.01, symbol="XRP-USDT")
    assert t.violations == []
    methods = [m["method"] for m in t.sent]
    seed = next(i for i, m in enumerate(t.sent)
                if "ma:anon:symbol" in m["params"].get("source", ""))
    assert methods.index("Page.enable") < seed < methods.index("Page.navigate")
    assert report.issues == []


class _ErrorTransport(FakeTransport):
    def __init__(self, failing: str) -> None:
        super().__init__([])
        self.failing = failing

    async def send(self, msg: dict[str, Any]) -> None:
        if msg["method"] == self.failing:
            self.sent.append(msg)
            self.queue.put_nowait({"id": msg["id"], "error": {"message": "boom"}})
            return
        await super().send(msg)


@pytest.mark.parametrize("method", ["Page.enable", "Page.addScriptToEvaluateOnNewDocument",
                                    "Page.navigate"])
async def test_setup_error_reply_means_check_not_done(method: str) -> None:
    t = _ErrorTransport(method)
    with pytest.raises(bc.EnvironmentNotReadyError, match=method):
        await bc.run_check(t, "http://127.0.0.1:8765/", 0.01, symbol="XRP-USDT")
    methods = [m["method"] for m in t.sent]
    assert methods[-1] == method                  # после отказа — ни одной команды
    if method != "Page.navigate":
        assert "Page.navigate" not in methods     # навигации без готовой сессии нет


async def test_seed_missing_is_own_error_not_chart_issue() -> None:
    t = FakeTransport([], state=_loaded(charts=0, seed=None))
    with pytest.raises(bc.EnvironmentNotReadyError, match=bc.SEED_MISSING) as exc:
        await bc.run_check(t, "http://127.0.0.1:8765/", 0.01, symbol="XRP-USDT")
    assert "XRP-USDT" in str(exc.value) and "графиков" not in str(exc.value)
    # Без --symbol метка не нужна.
    report = await bc.run_check(FakeTransport([]), "http://127.0.0.1:8765/", 0.01)
    assert report.issues == []


def test_seed_missing_exits_2(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                              capsys: pytest.CaptureFixture[str]) -> None:
    async def fake_check(url: str, chrome: Path, wait: float, symbol: str | None = None,
                         local_sdk: bool = True) -> bc.Report:
        raise bc.seed_missing("XRP-USDT")

    monkeypatch.setattr(bc, "check", fake_check)
    assert bc.main(["--chrome", str(chrome_exe), "--symbol", "XRP-USDT"]) == 2
    err = capsys.readouterr().err
    assert "Проверка не проведена: SEED_MISSING: монета XRP-USDT" in err


def test_csp_listener_marks_page_and_missing_is_issue() -> None:
    assert bc.CSP_LISTENER.endswith(" window.__maCspListener = true;")
    assert "csp_listener: window.__maCspListener === true" in bc.STATE_EXPR
    assert "seed: window.__maSeed === undefined ? null : window.__maSeed" in bc.STATE_EXPR
    issues = bc.Report(bc.Collector(), _loaded(csp_listener=False)).issues
    assert issues == ["слушатель CSP не дошёл до страницы — нарушения CSP не проверены"]
