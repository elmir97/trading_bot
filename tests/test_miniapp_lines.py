"""Линии открытой позиции на экране рынка (П.2) — по тексту `miniapp/chart.js`,
`market.js`, `index.html` (JS-раннера нет; поведение в браузере —
`miniapp_browser_check --scenario market_lines|market_lines_none|confirm_lines`):

- линии рисуются на видимой серии: скрытая серия прячет свои price lines, поэтому
  setType переносит их со свечей на «линию» и обратно (починка из плана П.2);
- рынок читает GET /api/positions своей цепочкой не чаще серверного кэша, при каждой
  загрузке монеты (вход, смена монеты/ТФ, возврат с итога сделки) — свежий запрос;
- позиции нет / другая монета — линий нет, смена монеты снимает старые.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.api import positions as api_positions
from tests.test_miniapp_market import _body, _const

_MINIAPP = Path(__file__).resolve().parents[1] / "miniapp"


def _js(name: str) -> str:
    return (_MINIAPP / name).read_text(encoding="utf-8")


# --- chart.js ------------------------------------------------------------------------


def test_lines_drawn_on_visible_series() -> None:
    js = _js("chart.js")
    draw = _body(js, "function drawLines(")
    assert 'const host = type === "line" ? line : candles;' in draw
    assert "host.createPriceLine(" in draw and "candles.createPriceLine" not in draw
    # Снимаются с той серии, на которой нарисованы, а не с текущей.
    assert "for (const [series, priceLine] of priceLines) series.removePriceLine(priceLine);" \
        in draw
    assert "container.dataset.lines =" in draw
    # Атрибут — серия, на которой линии на деле (сценарий market_lines ловит «не та серия»).
    assert 'container.dataset.linesOn = host === line ? "line" : "candles";' in draw


def test_set_type_redraws_lines() -> None:
    js = _js("chart.js")
    set_type = _body(js, "setType(next) {")
    assert "if (destroyed) return;" in set_type
    assert set_type.index("type = ") < set_type.index("drawLines();")
    assert 'candles.applyOptions({ visible: type === "candles" });' in set_type
    assert 'line.applyOptions({ visible: type === "line" });' in set_type


def test_destroy_clears_lines_attributes() -> None:
    destroy = _body(_js("chart.js"), "destroy() {")
    assert "delete container.dataset.lines;" in destroy
    assert "delete container.dataset.linesOn;" in destroy
    assert destroy.index("delete container.dataset.lines;") < destroy.index("chart.remove();")


# --- market.js -----------------------------------------------------------------------


def test_market_reads_positions_only() -> None:
    js = _js("market.js")
    calls = set(re.findall(r"\bapi\.(\w+)\(\s*[\"`]([^\"`?$]+)", js))
    assert ("get", "/api/positions") in calls
    assert {method for method, _ in calls} == {"get"}            # рынок ничего не пишет


def test_position_poll_chained_not_faster_than_cache() -> None:
    js = _js("market.js")
    assert "setInterval(" not in js
    assert _const(js, "POSITION_POLL_MS") == 10000
    assert _const(js, "POSITION_POLL_MS") >= api_positions.TTL_POSITIONS * 1000
    poll = _body(js, "async function pollPositions(")
    assert poll.startswith("{\n    posTimer = null;")
    assert "document.hidden" in poll and "!loaded" in poll
    assert "if (myGen !== gen) return;" in poll                     # ответ старой монеты
    assert "data.positions.filter((p) => p.symbol === symbol)" in poll
    schedule = _body(js, "function schedulePositions(")
    assert "document.hidden" in schedule and "posTimer !== null" in schedule
    assert "setTimeout(pollPositions, delay)" in schedule


def test_position_errors_keep_lines_and_401_fatal() -> None:
    poll = _body(_js("market.js"), "async function pollPositions(")
    catch = poll[poll.index("} catch (err) {"):]
    assert "if (fatal(err)) return;" in catch
    assert "posFailures += 1;" in catch
    for removes in ("setLines", "clearPosition", "renderPosition", "replaceChildren"):
        assert removes not in catch, removes                          # линии остаются
    assert "Линии позиции могут быть устаревшими" in catch


def test_fresh_positions_on_every_load_and_stop_everywhere() -> None:
    js = _js("market.js")
    load = _body(js, "async function load(")
    assert load.index("loaded = true;") < load.index("pollPositions();")
    assert load.index("pollPositions();") < load.index("await loadCandles(")
    stop = _body(js, "function stopPolling(")
    assert "clearTimeout(posTimer)" in stop and "posController.abort()" in stop
    # Смена монеты/ТФ (load → resetView) снимает линии прошлой монеты.
    assert "clearPosition();" in _body(js, "function resetView(")
    clear = _body(js, "function clearPosition(")
    assert "chart.setLines([])" in clear and "el.position.hidden = true;" in clear
    visibility = _body(js, "function onVisibility(")
    assert "pollPositions();" in visibility and "tick();" in visibility


def test_levels_and_summary() -> None:
    js = _js("market.js")
    levels = _body(js, "export function positionLevels(")
    for kind in ("entry", "stop", "take", "liq"):
        assert f'kind: "{kind}"' in levels
    assert "for (const o of p.stops)" in levels and "for (const o of p.takes)" in levels
    assert "if (p.liquidation_price !== null)" in levels
    assert 'const tag = positions.length > 1;' in levels              # хедж — сторона в подписи
    summary = _body(js, "export function positionSummary(")
    assert "без стопа" in summary


def test_legend_markup_and_link_to_positions() -> None:
    html = _js("index.html")
    for element_id in ("market-position", "market-position-list", "market-position-stale",
                       "market-position-open"):
        assert f'id="{element_id}"' in html
    assert re.search(r'id="market-position"[^>]*\bhidden\b', html)
    js = _js("market.js")
    assert "onPositions()" in js
    assert "onPositions: openPositions" in _js("app.js")
    render = _body(js, "function renderPosition(")
    assert "innerHTML" not in js and "textContent = positionSummary(p)" in render
