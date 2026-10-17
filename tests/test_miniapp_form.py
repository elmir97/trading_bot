"""Форма сделки Mini App (подэтап 2.4, docs/miniapp-plan.md §5а экран 2) — по тексту
`miniapp/form.js`, `render.js`, `chart.js`, `index.html` (JS-раннера в проекте нет;
в браузере — стенд + сценарий в headless Chrome, см. handoff):

- расчётов на клиенте нет: объём, риск $, RR, маржа, ликвидация — из ответа
  POST /api/open/preview, единственного запроса формы на запись-превью;
- плечо выше min(максимум стороны, max_leverage плана) выбрать нельзя: ползунок
  ограничен потолком, тело запроса — тоже (сервер отклоняет выше — test_api_open);
- без ключа или при сбое чтения максимума — «Подключи ключи биржи», превью и кнопки нет;
- риск по умолчанию — из плана, клиент его не меняет и не запоминает;
- BLOCK — кнопки нет, WARN — «Открыть всё равно» (решение 05.10);
- живой пересчёт: 300 мс после последнего изменения, «последний побеждает».
"""

from __future__ import annotations

import re
from pathlib import Path

_MINIAPP = Path(__file__).resolve().parents[1] / "miniapp"


def _js(name: str) -> str:
    return (_MINIAPP / name).read_text(encoding="utf-8")


def _body(text: str, start: str) -> str:
    i = text.index(start)
    i = i + len(start) - 1 if start.endswith("{") else text.index("{", i)
    depth = 0
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i:j + 1]
    raise AssertionError(start)


# --- плечо -----------------------------------------------------------------------------


def test_leverage_cap_is_min_of_side_max_and_plan() -> None:
    cap = _body(_js("render.js"), "export function leverageCap(")
    assert "if (!leverage) return null;" in cap
    assert 'side === "SHORT" ? leverage.max_short : leverage.max_long' in cap
    assert "Math.min(sideMax, planMax)" in cap


def test_leverage_above_cap_cannot_be_chosen() -> None:
    """Выше потолка — нельзя: clampLeverage режет, ползунок max = потолок, в тело
    запроса плечо идёт только через clampLeverage."""
    clamp = _body(_js("render.js"), "export function clampLeverage(")
    assert clamp.rstrip("}").rstrip().endswith("return Math.min(n, cap);")
    assert "if (!Number.isFinite(n) || n < 1) return 1;" in clamp
    form = _js("form.js")
    assert "el.leverage.max = String(cap);" in _body(form, "function renderControls()")
    handler = _body(form, 'el.leverage.addEventListener("input", () => {')
    assert "leverage = clampLeverage(el.leverage.value, cap);" in handler
    body = _body(form, "function buildBody()")
    assert "leverage: clampLeverage(leverage, cap)," in body
    # Предложенное сервером и текущее плечо биржи — тоже через потолок.
    assert "leverage = clampLeverage(leverage, cap);" in _body(form, "function applyCap()")
    run = _body(form, "async function runPreview()")
    assert "clampLeverage(next.calc.suggested_leverage, cap)" in run
    # Разметка: ползунок целый, от 1; max ставит JS.
    html = _js("index.html")
    assert re.search(r'<input type="range" class="field-range" id="trade-leverage" min="1"', html)


def test_no_key_or_unread_max_blocks_opening() -> None:
    render = _js("render.js")
    assert 'export const NO_KEY_TEXT = "Подключи ключи биржи";' in render
    form = _js("form.js")
    apply = _body(form, "function applyCap()")
    assert "leverageCap(market.leverage, side, me().plan.max_leverage)" in apply
    assert "blocked(NO_KEY_TEXT, reason);" in apply
    blocked = _body(form, "function blocked(title, detail)")
    for line in ("el.form.hidden = true;", "cancelPreview();", "card = null;", "openButton();"):
        assert line in blocked
    # Без потолка нет ни превью, ни тела, ни кнопки.
    assert "if (!active || !cap) return;" in _body(form, "function schedulePreview(")
    assert "if (bad.length || !cap) return null;" in _body(form, "function buildBody()")
    assert "pending || !cap ? { visible: false }" in _body(form, "function openButton()")
    assert "if (!active || pending || !cap) return;" in _body(form, "function onOpenClick()")


# --- расчёты только на сервере ------------------------------------------------------------


def test_form_posts_only_preview() -> None:
    form = _js("form.js")
    assert re.findall(r"api\.post\(([^,]+),", form) == ['"/api/open/preview"']
    assert "/api/open\"" not in form and "/confirm" not in form


def test_client_does_not_compute_money() -> None:
    """Number — только для плеча (целое) и срока лимита; суммы, цены, RR — строки сервера."""
    render = _js("render.js")
    assert len(re.findall(r"\bNumber\(", render)) == 1
    assert "Math.trunc(Number(value))" in render
    assert re.search(r"\bparseFloat\b|\bparseInt\b|\btoFixed\b", render) is None
    form = _js("form.js")
    assert re.findall(r"\bNumber\([^)]*\)", form) == ["Number(el.expiry.value)"]
    assert re.search(r"\bparseFloat\b|\bparseInt\b|\btoFixed\b", form) is None
    preview = _body(render, "{ symbol, marginType }) {")
    for field in ("c.quantity", "c.notional", "c.margin", "c.rr", "c.liq_estimate",
                  "c.fee_usd", "c.risk_usd", "c.risk_percent"):
        assert field in preview, field


def test_amount_input_is_validated_not_converted() -> None:
    parse = _body(_js("render.js"), "export function parseAmount(")
    assert '.replace(",", ".")' in parse                      # «1,5» с клавиатуры iOS
    assert r"/^\d+(\.\d+)?$/" in parse
    assert "return value;" in parse                           # строкой, без Number


def test_chart_lines_come_from_preview() -> None:
    lines = _body(_js("render.js"), "export function chartLines(")
    for source in ("card.calc.entry_price", "card.levels.stop_loss", "card.levels.take_profit",
                   "card.calc.liq_estimate"):
        assert source in lines
    form = _js("form.js")
    assert "chart.setLines(chartLines(card))" in _body(form, "function showCard(")
    chart = _js("chart.js")
    draw = _body(chart, "function drawLines()")
    assert "series.removePriceLine(priceLine)" in draw
    assert "host.createPriceLine(" in draw          # видимая серия (П.2, test_miniapp_lines)
    # Форма — свой компактный график, тот же модуль: без встроенного логотипа.
    assert "createPriceChart(el.chart" in form


# --- риск ------------------------------------------------------------------------------


def test_risk_default_is_plan_and_never_raised_by_client() -> None:
    form = _js("form.js")
    reset = _body(form, "function resetFields()")
    assert ("el.risk.value = settings.plan.risk_percent === null ? \"\" "
            ": String(settings.plan.risk_percent);") in reset
    # Значение риска пишет только сброс формы (открытие), дальше — пользователь.
    assert len(re.findall(r"el\.risk\.value\s*=", form)) == 1
    assert "storage.save" not in form
    assert 'riskKind = "percent";' in reset
    assert "resetFields();" in _body(form, "open(nextSide, nextSymbol) {")


# --- кнопка ----------------------------------------------------------------------------


def test_block_hides_button_and_warn_is_open_anyway() -> None:
    action = _body(_js("render.js"), "export function openAction(")
    assert ('if (!card || !card.calc || !card.can_open) return { visible: false, text: "", '
            "warn: false };") in action
    assert 'if (card.has_warnings) return { visible: true, text: "Открыть всё равно"' in action
    assert action.index("!card.can_open") < action.index("card.has_warnings")


def test_open_click_hands_last_preview_to_next_step_without_sending() -> None:
    click = _body(_js("form.js"), "function onOpenClick()")
    assert "onOpen({ body: cardBody, card });" in click
    assert "api." not in click
    app = _js("app.js")
    assert "onOpen: openConfirm" in app
    assert 'show("confirm");' in _body(app, "function openConfirm(payload)")


# --- живой пересчёт ------------------------------------------------------------------------


def test_preview_debounce_and_last_wins() -> None:
    form = _js("form.js")
    assert "export const PREVIEW_DEBOUNCE_MS = 300;" in form
    schedule = _body(form, "function schedulePreview(")
    assert "clearTimeout(previewTimer)" in schedule
    assert "previewTimer = setTimeout(runPreview, delay);" in schedule
    assert 'el.result.classList.add("pending");' in schedule
    run = _body(form, "async function runPreview()")
    assert "const seq = ++previewSeq;" in run
    assert "if (previewController) previewController.abort();" in run
    after = run[run.index("await api.post("):]
    assert after.index("if (seq !== previewSeq) return;") < after.index("showCard(next, body);")
    assert "if (seq !== previewSeq || (err && err.name === \"AbortError\")) return;" in run


def test_every_field_change_recomputes() -> None:
    form = _js("form.js")
    assert "input.addEventListener(\"input\", changed);" in form
    assert "for (const input of [el.limit, el.stop, el.take, el.risk])" in form
    assert 'el.expiry.addEventListener("change", changed);' in form
    assert "schedulePreview();" in _body(form, "function changed()")
    # Маркет: новая цена → пересчёт.
    assert '(moved && entry === "MARKET")' in _body(form, "async function tick()")


def test_leave_stops_everything() -> None:
    leave = _body(_js("form.js"), "leave() {")
    for line in ("stopPolling();", "cancelPreview();", "chart.destroy();",
                 'document.removeEventListener("visibilitychange", onVisibility);',
                 "tg.MainButton.offClick(onOpenClick);"):
        assert line in leave


def test_sdk_screen_has_no_duplicate_title() -> None:
    html = _js("index.html")
    sdk = html[html.index('id="screen-sdk"'):]
    sdk = sdk[:sdk.index("</section>")]
    assert "<h1" not in sdk


def test_take_2r_chip_uses_server_price() -> None:
    """Чип «2R» — цена тейка из ответа превью (calc.take_2r), клиент её не считает."""
    form = _js("form.js")
    assert 'return card && card.calc && card.calc.take_2r ? String(card.calc.take_2r) : null;' \
        in _body(form, "function take2r()")
    handler = _body(form, 'el.takeChips.addEventListener("click", (event) => {\n'
                          '    const button = event.target.closest("button[data-r]");')
    assert "el.take.value = price;" in handler and 'takeKind = "price";' in handler
    assert "if (!active || !button || !price) return;" in handler
    assert "el.take2r.disabled = !take2r();" in _body(form, "function renderControls()")
    assert '<button type="button" data-r="2" id="trade-take-2r" disabled>2R</button>' \
        in _js("index.html")
