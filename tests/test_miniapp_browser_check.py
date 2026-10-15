"""`scripts/miniapp_browser_check.py` без настоящего Chrome: разбор событий
DevTools, порядок команд, ответ evaluate только по своему id, коды выхода."""

from __future__ import annotations

import asyncio
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
        self.state = state if state is not None else {"visible_screens": ["screen-market"]}

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
    assert report.state == {"visible_screens": ["screen-market"]}
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


def _report(*events: dict[str, Any], state: Any = "ok") -> bc.Report:
    c = bc.Collector()
    for e in events:
        c.feed(e)
    return bc.Report(c, state)


def test_exit_0_when_clean(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                           capsys: pytest.CaptureFixture[str]) -> None:
    async def fake_check(url: str, chrome: Path, wait: float) -> bc.Report:
        return _report(_response(200, url))

    monkeypatch.setattr(bc, "check", fake_check)
    assert bc.main(["--chrome", str(chrome_exe)]) == 0
    assert "замечаний нет" in capsys.readouterr().out


def test_exit_1_lists_issues(monkeypatch: pytest.MonkeyPatch, chrome_exe: Path,
                             capsys: pytest.CaptureFixture[str]) -> None:
    async def fake_check(url: str, chrome: Path, wait: float) -> bc.Report:
        return _report(_console("error", "boom"), _response(404, url + "x.js"))

    monkeypatch.setattr(bc, "check", fake_check)
    assert bc.main(["--chrome", str(chrome_exe)]) == 1
    out = capsys.readouterr().out
    assert "✗ console.error: boom" in out
    assert "✗ HTTP 404" in out


@pytest.mark.parametrize("argv", [
    ["--url", "http://localhost:8765/"],          # не 127.0.0.1
    [],                                           # Chrome не задан
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
