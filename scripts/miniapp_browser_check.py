"""Проверка страницы стенда Mini App в headless Chrome (DevTools-протокол).

    python -m scripts.miniapp_browser_check [--url http://127.0.0.1:8765/]
        [--chrome <путь к Chrome>] [--wait 8] [--symbol XRP-USDT]
    python -m scripts.miniapp_browser_check --scenario <имя>|all [--url …] [--chrome …]
    python -m scripts.miniapp_browser_check --shots <папка вне репозитория> [--shot <экран>|all]

`--scenario` — сценарии браузера (scripts/miniapp_scenarios.py: рынок, SDK, свечи,
форма сделки): каждый — свой свежий Chrome, итог по сценарию, exit 1 — есть замечания.

Путь к Chrome — `--chrome` или переменная `MINIAPP_CHROME`, по умолчанию его нет.
Каждый запуск — свежий временный профиль (иначе Chrome исполняет закэшированный
ES-модуль, CLAUDE.md), после проверки профиль удаляется. Кэш выключен.

Собирает: console (warning и error — замечание), исключения страницы,
нарушения CSP (записи Log и событие securitypolicyviolation), статусы сетевых
запросов (≥ 400 и сбой загрузки — замечание), итог — видимые экраны и число
графиков. Со свежим профилем монеты нет — Mini App открывает поиск, графика нет;
`--symbol` кладёт монету в localStorage стенда до скриптов страницы — открывается
экран рынка, и график обязан быть ровно один (иначе замечание). Сид оставляет метку;
метки нет — exit 2 `SEED_MISSING` (проверка не состоялась), а не «графиков 0».
Команды до навигации ждут своего ответа (иначе навигация обгоняет сид, 25.10).

Только 127.0.0.1: и URL страницы, и DevTools Chrome (`--remote-debugging-address`).
Коды выхода: 0 — чисто; 1 — есть замечания (список в выводе); 2 — окружение не
готово (нет Chrome, стенд не отвечает, URL не 127.0.0.1). В образ не попадает
(`.dockerignore`).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import aiohttp

LOCALHOST = "127.0.0.1"
DEFAULT_URL = f"http://{LOCALHOST}:8765/"
CHROME_ENV = "MINIAPP_CHROME"
DEFAULT_WAIT = 8.0
STATE_TIMEOUT = 5.0
START_TIMEOUT = 15.0
CHART_POLL = 0.5                 # --symbol: дочитывание состояния, пока график не появился

# До любого скрипта страницы: нарушение CSP — строкой в console.error. Метка
# window.__maCspListener — слушатель дошёл до документа (STATE_EXPR, см. run_check).
CSP_MARK = "CSP-VIOLATION"
CSP_LISTENER = (
    "document.addEventListener('securitypolicyviolation', e => console.error("
    f"'{CSP_MARK}', e.violatedDirective, e.blockedURI, e.sourceFile + ':' + e.lineNumber));"
    " window.__maCspListener = true;"
)
# Сид --symbol не дошёл до страницы (25.10: ~половина прогонов) — проверка экрана рынка не
# состоялась: отказ с этим кодом (exit 2), а не замечание «графиков 0».
SEED_MISSING = "SEED_MISSING"
# Ключ «последней монеты» Mini App: storage.js — префикс ma и telegram_id, вне
# Telegram его нет → anon; market.js — имя symbol, значение JSON-строкой.
STORAGE_SYMBOL_KEY = "ma:anon:symbol"
# initData стенда: вне Telegram SDK берёт её из хеша адреса (tgWebAppData). Стенд её не
# проверяет (current_user подменён), но пустая initData — экран «Открой из бота» (app.js).
# Без user: хранилище — анонимное (ma:anon:…), как у сценариев.
STAND_INIT_DATA = "query_id=stand&auth_date=0&hash=stand"
# SDK telegram.org на стенде — локальная копия по умолчанию (Д.4, решение владельца 18.10):
# сетевые срывы до telegram.org не валят гейт. Chrome получает её через Fetch вместо сети;
# index.html и прод не меняются. Настоящий SDK — флаг --real-sdk и сценарий sdk_real.
SDK_URL = "https://telegram.org/js/telegram-web-app.js"
STAND_SDK = Path(__file__).resolve().parent / "stand_sdk" / "telegram-web-app.js"


def stand_sdk_text() -> str:
    return STAND_SDK.read_text(encoding="utf-8")


def sdk_fulfill(request_id: str) -> dict[str, Any]:
    """Ответ Fetch.fulfillRequest локальной копией SDK."""
    return {"requestId": request_id, "responseCode": 200,
            "responseHeaders": [{"name": "Content-Type", "value": "application/javascript"}],
            "body": base64.b64encode(STAND_SDK.read_bytes()).decode()}


def page_url(url: str, *, version: str | None = None, init_data: bool = True,
             theme_params: dict[str, str] | None = None) -> str:
    """Адрес страницы стенда с параметрами Telegram в хеше. theme_params — как у клиента
    Telegram (SDK берёт их из хеша и по bg_color решает colorScheme)."""
    params = []
    if init_data:
        params.append("tgWebAppData=" + quote(STAND_INIT_DATA, safe=""))
    if version is not None:
        params.append(f"tgWebAppVersion={version}")
    if theme_params is not None:
        params.append("tgWebAppThemeParams=" + quote(json.dumps(theme_params), safe=""))
    return url + ("#" + "&".join(params) if params else "")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,24}-USDT$")
STATE_EXPR = """(() => ({
  title: document.title,
  visible_screens: [...document.querySelectorAll('.screen')].filter(s => !s.hidden).map(s => s.id),
  charts: document.querySelectorAll('.tv-lightweight-charts').length,
  lightweight_charts: window.LightweightCharts ? window.LightweightCharts.version() : null,
  telegram_sdk: typeof window.Telegram === 'object',
  csp_listener: window.__maCspListener === true,
  seed: window.__maSeed === undefined ? null : window.__maSeed,
}))()"""


class EnvironmentNotReadyError(Exception):
    """Проверку нельзя провести — exit 2."""


def require_localhost(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "ws") or parts.hostname != LOCALHOST:
        raise EnvironmentNotReadyError(f"только {LOCALHOST}: {url}")
    return url


def chrome_path(arg: str | None, env: dict[str, str] | None = None) -> Path:
    raw = arg or (os.environ if env is None else env).get(CHROME_ENV)
    if not raw:
        raise EnvironmentNotReadyError(f"путь к Chrome не задан: --chrome или {CHROME_ENV}")
    path = Path(raw)
    if not path.is_file():
        raise EnvironmentNotReadyError(f"Chrome не найден: {path}")
    return path


# --- разбор событий DevTools (без Chrome) -----------------------------------------


def _args_text(args: list[dict[str, Any]]) -> str:
    return " ".join(str(a.get("value", a.get("description", ""))) for a in args)


def _frames(params: dict[str, Any]) -> str:
    frames = params.get("stackTrace", {}).get("callFrames", [])[:3]
    return " < ".join(f"{f['url'].rsplit('/', 1)[-1]}:{f['lineNumber'] + 1}" for f in frames)


@dataclass
class Collector:
    """Сообщения DevTools → события. Ответы на команды — только по своему id
    (сообщение без id — событие, не ответ: 15.10 событие принималось за ответ
    на ещё не отправленный evaluate, когда его id был None)."""

    console: list[tuple[str, str]] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    csp: list[str] = field(default_factory=list)
    log: list[tuple[str, str]] = field(default_factory=list)
    network: list[tuple[int | None, str]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    responses: dict[int, dict[str, Any]] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)      # requestId → адрес

    def feed(self, msg: dict[str, Any]) -> None:
        if isinstance(msg.get("id"), int):
            self.responses[msg["id"]] = msg
            return
        method = msg.get("method")
        p = msg.get("params", {})
        if method == "Runtime.consoleAPICalled":
            text = _args_text(p.get("args", []))
            if text.startswith(CSP_MARK):
                self.csp.append(text)
            else:
                where = _frames(p)
                self.console.append((p.get("type", ""), text + (f" @ {where}" if where else "")))
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            self.exceptions.append(d.get("exception", {}).get("description") or d.get("text", ""))
        elif method == "Log.entryAdded":
            e = p.get("entry", {})
            text = e.get("text", "")
            if "Content Security Policy" in text:
                self.csp.append(text)
            else:
                where = f" ({e['url']})" if e.get("url") else ""
                self.log.append((e.get("level", ""), f"{e.get('source', '')}: {text}{where}"))
        elif method == "Network.requestWillBeSent":
            self.urls[p.get("requestId", "")] = p.get("request", {}).get("url", "")
        elif method == "Network.responseReceived":
            r = p.get("response", {})
            self.network.append((r.get("status"), r.get("url", "")))
        elif method == "Network.loadingFailed":
            if not p.get("canceled"):
                reason = p.get("blockedReason") or ""
                url = self.urls.get(p.get("requestId", ""), "")
                self.failed.append(" ".join(x for x in (p.get("errorText", ""), reason, url) if x))

    def issues(self) -> list[str]:
        out = [f"CSP: {t}" for t in self.csp]
        out += [f"исключение: {t}" for t in self.exceptions]
        out += [f"console.{k}: {t}" for k, t in self.console if k in ("error", "warning", "assert")]
        out += [f"log.{lvl}: {t}" for lvl, t in self.log if lvl in ("error", "warning")]
        out += [f"HTTP {s}: {u}" for s, u in self.network if s is None or s >= 400]
        out += [f"загрузка не удалась: {t}" for t in self.failed]
        return out


class Transport(Protocol):
    async def send(self, msg: dict[str, Any]) -> None: ...
    async def receive(self) -> dict[str, Any]: ...


@dataclass
class Report:
    collector: Collector
    state: Any
    expect_chart: bool = False

    @property
    def issues(self) -> list[str]:
        problems = self.collector.issues()
        if self.state is None:
            problems.append("состояние страницы не прочитано (evaluate не ответил)")
        else:
            problems += page_problems(self.state, expect_chart=self.expect_chart)
        return problems


def page_problems(state: Any, *, expect_chart: bool) -> list[str]:
    """Страница не догрузилась — тоже замечание: 16.10 SDK с telegram.org иногда не
    успевал за --wait, экранов не было, а без этой проверки итог был «чисто»."""
    if not isinstance(state, dict):
        return [f"состояние страницы не разобрано: {state!r}"]
    out = []
    if not state.get("visible_screens"):
        out.append("ни один экран не показан — страница не догрузилась (увеличь --wait)")
    if not state.get("telegram_sdk"):
        out.append("SDK Telegram не загрузился")
    if not state.get("lightweight_charts"):
        out.append("библиотека графиков не загрузилась")
    if not state.get("csp_listener"):
        out.append("слушатель CSP не дошёл до страницы — нарушения CSP не проверены")
    if expect_chart and state.get("charts") != 1:
        out.append(f"графиков на странице {state.get('charts')}, ждали 1 (--symbol)")
    return out


def require_symbol(symbol: str) -> str:
    if not SYMBOL_RE.match(symbol):
        raise EnvironmentNotReadyError(f"--symbol {symbol!r}: нужен контракт вида XRP-USDT")
    return symbol


def seed_script(symbol: str) -> str:
    """Монета в localStorage до скриптов страницы. Метка window.__maSeed — только после
    записи: хранилище недоступно или скрипт не выполнился — метки нет (SEED_MISSING)."""
    value = json.dumps(json.dumps(require_symbol(symbol)))
    return (f"try {{ localStorage.setItem({json.dumps(STORAGE_SYMBOL_KEY)}, {value}); "
            f"window.__maSeed = {json.dumps(symbol)}; }} catch (e) {{}}")


def seed_missing(symbol: str) -> EnvironmentNotReadyError:
    return EnvironmentNotReadyError(
        f"{SEED_MISSING}: монета {symbol} не попала в localStorage до скриптов страницы — "
        "экран рынка не проверен (сбой обвязки DevTools, не страницы); повтори запуск")


async def run_check(transport: Transport, url: str, wait: float, *, symbol: str | None = None,
                    clock: Callable[[], float] = time.monotonic,
                    local_sdk: bool = True) -> Report:
    collector = Collector()
    seq = 0

    async def call(method: str, params: dict[str, Any] | None = None) -> int:
        nonlocal seq
        seq += 1
        await transport.send({"id": seq, "method": method, "params": params or {}})
        return seq

    async def pump(seconds: float, until: Callable[[], bool] = lambda: False) -> None:
        deadline = clock() + seconds
        while not until():
            left = deadline - clock()
            if left <= 0:
                return
            try:
                msg = await asyncio.wait_for(transport.receive(), left)
            except TimeoutError:
                return
            collector.feed(msg)
            if msg.get("method") == "Fetch.requestPaused":
                rid = msg["params"]["requestId"]
                if msg["params"].get("request", {}).get("url") == SDK_URL:
                    await call("Fetch.fulfillRequest", sdk_fulfill(rid))
                else:
                    await call("Fetch.continueRequest", {"requestId": rid})

    async def command(method: str, params: dict[str, Any] | None = None) -> None:
        """Команда до навигации — с ожиданием своего ответа. 25.10: без ожидания
        Page.navigate (его исполняет процесс браузера, переход about:blank → стенд — в
        новый процесс рендерера) обгонял Page.enable и addScriptToEvaluateOnNewDocument
        свежей вкладки: документ грузился без сида, слушателя CSP и событий Page."""
        my = await call(method, params)
        await pump(STATE_TIMEOUT, lambda: my in collector.responses)
        reply = collector.responses.get(my)
        if reply is None:
            raise EnvironmentNotReadyError(f"DevTools: {method} не ответил за {STATE_TIMEOUT:g} с")
        if "error" in reply:
            raise EnvironmentNotReadyError(f"DevTools: {method} — {reply['error']}")

    for method in ("Runtime.enable", "Log.enable", "Network.enable", "Page.enable"):
        await command(method)
    await command("Network.setCacheDisabled", {"cacheDisabled": True})
    if local_sdk:
        await command("Fetch.enable", {"patterns": [
            {"urlPattern": "*telegram.org*", "requestStage": "Request"}]})
    await command("Page.addScriptToEvaluateOnNewDocument", {"source": CSP_LISTENER})
    if symbol is not None:
        await command("Page.addScriptToEvaluateOnNewDocument", {"source": seed_script(symbol)})
    started = clock()
    await command("Page.navigate", {"url": page_url(url)})
    await pump(max(0.0, wait - (clock() - started)))

    async def read_state() -> Any:
        eval_id = await call("Runtime.evaluate", {"expression": STATE_EXPR, "returnByValue": True})
        await pump(STATE_TIMEOUT, lambda: eval_id in collector.responses)
        reply = collector.responses.get(eval_id)
        if reply is None or "error" in reply:
            return None
        return reply.get("result", {}).get("result", {}).get("value")

    state = await read_state()
    if symbol is not None and isinstance(state, dict) and state.get("seed") != symbol:
        raise seed_missing(symbol)
    # С --symbol график обязан появиться: полное ожидание wait уже прошло (замечания console
    # собраны), но после тяжёлого прогона (--scenario all) первая загрузка иногда дольше 8 с —
    # дочитываем состояние до START_TIMEOUT. Не появился и тогда — замечание, как раньше.
    deadline = clock() + START_TIMEOUT
    while (symbol is not None and isinstance(state, dict) and state.get("charts") != 1
           and clock() < deadline):
        await pump(CHART_POLL)
        state = await read_state()
    return Report(collector, state, expect_chart=symbol is not None)


# --- Chrome и стенд ----------------------------------------------------------------


@contextmanager
def fresh_profile() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="miniapp-chrome-"))
    try:
        yield path
    finally:
        for _ in range(20):            # Windows держит файлы профиля ещё миг после выхода
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                break
            time.sleep(0.25)
        if path.exists():
            print(f"⚠️ профиль Chrome не удалён: {path}", file=sys.stderr)


def chrome_command(chrome: Path, profile: Path) -> list[str]:
    return [
        str(chrome), "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions",
        f"--user-data-dir={profile}",
        f"--remote-debugging-address={LOCALHOST}", "--remote-debugging-port=0",
        "--window-size=390,844", "about:blank",
    ]


async def _devtools_port(profile: Path, proc: subprocess.Popen[bytes]) -> int:
    marker = profile / "DevToolsActivePort"
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise EnvironmentNotReadyError(f"Chrome завершился сразу (код {proc.returncode})")
        try:
            return int(marker.read_text(encoding="utf-8").splitlines()[0])
        except (OSError, ValueError, IndexError):
            await asyncio.sleep(0.1)
    raise EnvironmentNotReadyError("Chrome не открыл DevTools за 15 с")


async def stand_answers(http: aiohttp.ClientSession, url: str) -> None:
    try:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
            if r.status != 200:
                raise EnvironmentNotReadyError(f"стенд ответил {r.status}: {url}")
    except aiohttp.ClientError as exc:
        raise EnvironmentNotReadyError(f"стенд не отвечает: {url} ({type(exc).__name__})") from None
    except TimeoutError:
        raise EnvironmentNotReadyError(f"стенд не отвечает: {url} (таймаут)") from None


class _WsTransport:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws

    async def send(self, msg: dict[str, Any]) -> None:
        await self._ws.send_json(msg)

    async def receive(self) -> dict[str, Any]:
        data: dict[str, Any] = await self._ws.receive_json()
        return data


@asynccontextmanager
async def chrome_page(http: aiohttp.ClientSession, chrome: Path) -> AsyncIterator[Transport]:
    """Свежий Chrome (временный профиль), одна вкладка — транспорт DevTools."""
    with fresh_profile() as profile:
        proc = subprocess.Popen(chrome_command(chrome, profile),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port = await _devtools_port(profile, proc)
            async with http.put(f"http://{LOCALHOST}:{port}/json/new?about:blank") as r:
                target = await r.json()
            ws_url = require_localhost(target["webSocketDebuggerUrl"])
            async with http.ws_connect(ws_url, max_msg_size=0) as ws:
                yield _WsTransport(ws)
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)


async def check(url: str, chrome: Path, wait: float, symbol: str | None = None,
                local_sdk: bool = True) -> Report:
    async with aiohttp.ClientSession() as http:
        await stand_answers(http, url)
        async with chrome_page(http, chrome) as transport:
            return await run_check(transport, url, wait, symbol=symbol, local_sdk=local_sdk)


async def check_scenarios(url: str, chrome: Path, items: list[Any]) -> list[tuple[Any, Any]]:
    """Сценарии по очереди, каждый — свой Chrome (один браузер за раз — память)."""
    from scripts import miniapp_scenarios as ms

    out: list[tuple[Any, Any]] = []
    async with aiohttp.ClientSession() as http:
        await stand_answers(http, url)

        async def reset() -> None:
            async with http.post(url + "dev/reset",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    raise ms.ScenarioError(f"сброс стенда: HTTP {r.status}")

        async def seed(preset: str) -> None:
            async with http.post(url + "dev/positions", params={"preset": preset},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    raise ms.ScenarioError(f"позиции стенда ({preset}): HTTP {r.status}")

        for item in items:
            async with chrome_page(http, chrome) as transport:
                page = await ms.run_one(transport, url, item, reset, seed)
            out.append((item, page))
            print(ms.render(item, page), flush=True)
    return out


async def check_shots(url: str, chrome: Path, items: list[Any],
                      directory: Path) -> list[tuple[Any, str, Any, list[Path]]]:
    """Снимки экранов (scripts/miniapp_shots.py): экран × тема — свой Chrome."""
    from scripts import miniapp_shots as mshots

    out: list[tuple[Any, str, Any, list[Path]]] = []
    async with aiohttp.ClientSession() as http:
        await stand_answers(http, url)

        async def reset() -> None:
            async with http.post(url + "dev/reset",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    raise EnvironmentNotReadyError(f"сброс стенда: HTTP {r.status}")

        async def seed(preset: str) -> None:
            async with http.post(url + "dev/positions", params={"preset": preset},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    raise EnvironmentNotReadyError(f"позиции стенда ({preset}): HTTP {r.status}")

        for item in items:
            for theme in mshots.THEMES:
                async with chrome_page(http, chrome) as transport:
                    page, saved = await mshots.run_shot(transport, url, item, theme, directory,
                                                        reset, seed)
                out.append((item, theme, page, saved))
                print(mshots.render(item, theme, page, saved), flush=True)
    return out


def render(report: Report) -> str:
    c = report.collector
    lines = [f"запросы: {len(c.network)}, console: {len(c.console)}, CSP: {len(c.csp)}, "
             f"исключения: {len(c.exceptions)}",
             "состояние: " + json.dumps(report.state, ensure_ascii=False)]
    issues = report.issues
    lines += [f"✗ {i}" for i in issues] if issues else ["✓ замечаний нет"]
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--chrome", default=None)
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT)
    parser.add_argument("--symbol", default=None,
                        help="монета экрана рынка (localStorage стенда), напр. XRP-USDT")
    parser.add_argument("--scenario", default=None,
                        help="сценарий браузера (scripts/miniapp_scenarios.py) или all")
    parser.add_argument("--shots", default=None,
                        help="папка снимков экранов (вне репозитория), scripts/miniapp_shots.py")
    parser.add_argument("--shot", default="all", help="экран для --shots или all")
    parser.add_argument("--real-sdk", action="store_true",
                        help="SDK с telegram.org (по умолчанию — копия scripts/stand_sdk)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Консоль Windows (cp1251) не кодирует ✗/✓ — без PYTHONUTF8 не падать.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        url = require_localhost(args.url)
        symbol = None if args.symbol is None else require_symbol(args.symbol)
        if args.shots is not None:
            from scripts import miniapp_shots as mshots

            if args.scenario is not None:
                raise EnvironmentNotReadyError("--shots и --scenario — по отдельности")
            shots = mshots.select(args.shot)
            directory = mshots.out_dir(args.shots)
            chrome = chrome_path(args.chrome)
            taken = asyncio.run(check_shots(url, chrome, shots, directory))
            failed = [f"{item.name}-{theme}" for item, theme, page, saved in taken
                      if page.issues() or len(saved) != len(mshots.WIDTHS)]
            print(f"снимков {sum(len(s) for *_, s in taken)} в {directory}, с замечаниями "
                  f"{len(failed)}" + (f": {', '.join(failed)}" if failed else ""))
            return 1 if failed else 0
        if args.scenario is not None:
            from scripts import miniapp_scenarios as ms

            items = ms.select(args.scenario)
            chrome = chrome_path(args.chrome)
            results = asyncio.run(check_scenarios(url, chrome, items))
        else:
            chrome = chrome_path(args.chrome)
            report = asyncio.run(check(url, chrome, args.wait, symbol,
                                       local_sdk=not args.real_sdk))
    except EnvironmentNotReadyError as exc:
        print(f"✗ Проверка не проведена: {exc}", file=sys.stderr)
        return 2
    if args.scenario is not None:
        failed = [item.name for item, page in results if page.issues()]
        print(f"сценариев {len(results)}, с замечаниями {len(failed)}"
              + (f": {', '.join(failed)}" if failed else ""))
        return 1 if failed else 0
    print(render(report))
    return 1 if report.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
