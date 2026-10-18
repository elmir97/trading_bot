"""Экран рынка Mini App (подэтап 2.3, docs/miniapp-plan.md §5а) — по тексту
`miniapp/chart.js`, `market.js`, `app.js`, `index.html` (JS-раннера в проекте нет;
поведение в браузере — стенд + `scripts/miniapp_browser_check.py --symbol`):

- каждый график без встроенного логотипа, время UTC идёт в серию как есть, на оси
  и в перекрестье — локальное через форматтеры;
- цвета из themeParams, перекраска по themeChanged; размер — autoSize;
- каждая подписка снимается (themeChanged, matchMedia, visibilitychange, кнопки
  Telegram), при уходе с экрана — chart.remove();
- опрос — цепочкой setTimeout, не чаще серверного кэша цены (app/api/market.py),
  свечи — не чаще их кэша, пауза при ошибке растёт до потолка.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.api import market as api_market

_MINIAPP = Path(__file__).resolve().parents[1] / "miniapp"


def _js(name: str) -> str:
    return (_MINIAPP / name).read_text(encoding="utf-8")


def _const(text: str, name: str) -> int:
    match = re.search(rf"export const {name} = (\d+);", text)
    assert match, name
    return int(match.group(1))


def _body(text: str, start: str) -> str:
    """Тело блока от `start` до парной закрывающей скобки: `start`, кончающийся на
    «{», — эта скобка; иначе — первая после него."""
    i = text.index(start)
    i = i + len(start) - 1 if start.endswith("{") else text.index("{", i)
    depth = 0
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i:j + 1]
    raise AssertionError(start)


# --- chart.js -------------------------------------------------------------------------


def test_chart_has_no_builtin_logo_and_fills_container() -> None:
    chart = _js("chart.js")
    assert len(re.findall(r"\bcreateChart\s*\(", chart)) == 1
    options = _body(chart, "LWC.createChart(container,")
    assert re.search(r"attributionLogo:\s*false", options)
    assert re.search(r"autoSize:\s*true", options)


def test_chart_time_is_utc_and_shown_local() -> None:
    chart = _js("chart.js")
    # Данные — как пришли (unix UTC), без сдвига на пояс.
    assert re.search(r"time:\s*r\.time,", chart)
    assert "getTimezoneOffset" not in chart
    assert "timeZone" not in chart
    # Подписи — локальное время: форматтеры на оси и в перекрестье.
    options = _body(chart, "LWC.createChart(container,")
    assert "tickMarkFormatter:" in _body(options, "timeScale:")
    assert "timeFormatter:" in _body(options, "localization:")
    assert "new Date(time * 1000)" in chart
    assert "Intl.DateTimeFormat" in chart


def test_tick_formatter_never_falls_back_to_utc() -> None:
    """null из tickMarkFormatter — библиотека подпишет по UTC: всегда строка."""
    formatter = _body(_js("chart.js"), "tickMarkFormatter: (time, type) => {")
    assert "return null" not in formatter
    assert formatter.rstrip("}").rstrip().endswith("return clock.format(date);")


def test_chart_colors_from_tokens_and_follow_theme_changes() -> None:
    """Д.2: цвета — токены style.css (theme.js), не themeParams; смену темы шлёт theme.js."""
    chart = _js("chart.js")
    colors = _body(chart, "export function themeColors()")
    assert "themeParams" not in colors
    for name in ("--card", "--muted", "--chart-grid", "--ink", "--red", "--blue"):
        assert f'token("{name}")' in colors
    assert "document.addEventListener(THEME_EVENT, applyTheme)" in chart
    apply = _body(chart, "function applyTheme()")
    assert "themeColors()" in apply
    assert "chart.applyOptions" in apply


def test_chart_destroy_unsubscribes_and_removes() -> None:
    chart = _js("chart.js")
    destroy = _body(chart, "destroy() {")
    assert "document.removeEventListener(THEME_EVENT, applyTheme)" in destroy
    assert "chart.remove()" in destroy
    # Каждой подписке — отписка.
    assert chart.count("onEvent(") == chart.count("offEvent(")
    assert chart.count("addEventListener(") == chart.count("removeEventListener(")


# --- market.js ------------------------------------------------------------------------


def test_poll_is_not_faster_than_server_cache() -> None:
    market = _js("market.js")
    poll = _const(market, "PRICE_POLL_MS")
    candles = _const(market, "CANDLES_REFRESH_MS")
    backoff = _const(market, "MAX_BACKOFF_MS")
    assert poll >= api_market.TTL_PRICE * 1000          # чаще — та же цена из кэша
    assert poll <= 5000                                  # цена «раз в 3 с» (§5а)
    assert candles >= api_market.TTL_CANDLES_DEFAULT * 1000
    assert backoff >= 10 * poll
    assert _const(market, "SEARCH_DEBOUNCE_MS") >= 300


def test_poll_is_a_timeout_chain_with_one_request_in_flight() -> None:
    market = _js("market.js")
    assert "setInterval(" not in market
    schedule = _body(market, "function schedule()")
    assert "timer !== null" in schedule
    assert "document.hidden" in schedule
    assert "setTimeout(tick, delay)" in schedule
    assert "Math.min(PRICE_POLL_MS * 2 ** failures, MAX_BACKOFF_MS)" in schedule
    stop = _body(market, "function stopPolling()")
    assert "clearTimeout(timer)" in stop
    assert "controller.abort()" in stop


def test_candles_reload_only_on_new_candle_or_refresh() -> None:
    market = _js("market.js")
    tick = _body(market, "async function tick()")
    assert "lastTime + intervalSeconds(interval)" in tick
    assert 'lastTime === null ? "none" : quoted >= next ? next : null' in tick
    assert "candlesDue({ missing, asked, askedAt, loadedAt: candlesAt, now: Date.now() })" in tick
    assert "await loadCandles(myGen, signal, missing)" in tick
    # Цена двигает последнюю свечу только когда все свечи на месте.
    assert "} else if (missing === null) {" in tick
    assert tick.index("else if (missing === null)") < tick.index("chart.updateLast(m.last)")
    assert len(re.findall(r"loadCandles\(", tick)) == 1


def test_missing_candle_is_not_requested_every_tick() -> None:
    """2.3а: новая свеча запрошена и не пришла — не тянуть /candles на каждом опросе."""
    market = _js("market.js")
    retry = _const(market, "CANDLE_RETRY_MS")
    assert retry >= 3 * _const(market, "PRICE_POLL_MS")
    assert retry < _const(market, "CANDLES_REFRESH_MS")
    due = _body(market, "export function candlesDue({ missing, asked, askedAt, loadedAt, now }) {")
    assert "if (missing === null) return now - loadedAt >= CANDLES_REFRESH_MS;" in due
    assert "return missing !== asked || now - askedAt >= CANDLE_RETRY_MS;" in due
    # Что запрошено — помечает loadCandles; полная загрузка сбрасывает и помечает «нет свечей».
    load_candles = _body(market, "async function loadCandles(")
    assert load_candles.index("asked = missing;") < load_candles.index("await api.get(")
    assert "askedAt = Date.now();" in load_candles
    load = _body(market, "async function load()")
    assert "asked = null;" in load
    assert 'await loadCandles(myGen, signal, "none")' in load


def test_leverage_chip_is_contract_max_or_hidden() -> None:
    """«фьючерс · до N×» (шапка B, Д.3) — максимум контракта из /api/market
    (contract.max_leverage); нет ключа или сбой (null) — только «фьючерс»: ни «—», ни
    выдуманного числа."""
    render = _body(_js("market.js"), "function renderMarket(m)")
    assert "const maxLeverage = m.contract.max_leverage;" in render
    assert 'el.leverage.textContent = maxLeverage ? ` · до ${maxLeverage}×` : "";' in render
    assert "el.leverage.hidden = !maxLeverage;" in render
    assert "m.leverage" not in render


def test_stale_responses_are_dropped() -> None:
    """Ответ после смены монеты/ТФ или ухода с экрана не рисуется: проверка номера
    загрузки — сразу после каждого ответа API, до отрисовки."""
    market = _js("market.js")
    after = r"await api\.get\([^;]*;\s*if \(myGen !== gen(?: \|\| !chart)?\) return;"
    for name in ("async function tick()", "async function loadCandles("):
        body = _body(market, name)
        assert len(re.findall(r"await api\.get\(", body)) == len(re.findall(after, body)), name
    load = _body(market, "async function load()")
    check = load.index("if (myGen !== gen) return;", load.index("await api.get(marketPath()"))
    assert check < load.index("createPriceChart(") < load.index("renderMarket(m)")
    assert "seq !== searchSeq" in _body(market, "async function runSearch(")


def test_leave_releases_everything() -> None:
    market = _js("market.js")
    leave = _body(market, "leave() {")
    for call in ("stopPolling()", "cancelSearch()",
                 'document.removeEventListener("visibilitychange", onVisibility)',
                 "tg.MainButton.offClick(goLong)", "tg.SecondaryButton.offClick(goShort)",
                 "chart.destroy()", "chart = null", "gen += 1", "sideButtons(false)"):
        assert call in leave, call
    enter = _body(market, "enter() {")
    assert 'document.addEventListener("visibilitychange", onVisibility)' in enter
    assert "tg.MainButton.onClick(goLong)" in enter
    assert "tg.SecondaryButton.onClick(goShort)" in enter
    # Обработчики статичных элементов — один раз при создании экрана, не на каждом входе.
    assert "el." not in re.sub(r"el\.(search|head)\.hidden", "", enter)


def test_states_loading_empty_error() -> None:
    market = _js("market.js")
    assert 'chartState("Загрузка…")' in _body(market, "function resetView()")
    assert "Свечей нет" in _body(market, "async function loadCandles(")
    load = _body(market, "async function load()")
    assert "screenError(err)" in load
    assert "{ retry: true }" in load
    assert "status === 401" in _body(market, "function fatal(err)")
    assert "Цены могут быть устаревшими" in _body(market, "async function tick()")


def test_market_never_writes_html() -> None:
    market = _js("market.js")
    assert "textContent" in market
    assert re.search(r"innerHTML|outerHTML|insertAdjacentHTML", market) is None


# --- app.js и разметка ------------------------------------------------------------------


def test_show_leaves_previous_screen_before_entering_next() -> None:
    show = _body(_js("app.js"), "function show(name, { push = true } = {}) {")
    assert show.index("screens[current].leave()") < show.index("screens[name].enter()")


def test_market_markup_has_every_element_the_script_uses() -> None:
    html = _js("index.html")
    ids = set(re.findall(r'\$\("([a-z-]+)"\)', _js("market.js")))
    assert ids, "в market.js нет $(…)"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert missing == []
    for interval in ("1m", "5m", "15m", "1h", "4h", "1d"):
        assert f'data-interval="{interval}"' in html
    assert set(re.findall(r'data-interval="([^"]+)"', html)) == set(api_market.INTERVALS)
