"""Экран 5 Mini App — действия с позицией (5.2, план — §9а «Экран 5», «да» владельца 25.10) —
по тексту `miniapp/actions.js`, `positions.js`, `app.js`, `render.js`, `chart.js`, `index.html`
(JS-раннера нет; в браузере — сценарии `act_*` на стенде, 5.3):

- числа — только строки сервера: в JS ни одного пересчёта (время и паузы опроса — не числа
  сервера); чип стопа уходит строкой `level_percent`, перевод в цену — на сервере;
- превью без таймера; карточка — таймер от `ttl_seconds` ответа по монотонным часам, не от
  часов телефона; истёк — «Пересчитать», подтверждать нельзя;
- прямые действия — два нажатия (кнопка → карточка → MainButton); `showConfirm` — только LIVE
  и рост риска; рост риска — «⚠️ Да, увеличить риск», `risk_confirmed` = `card.risk_increase`;
- клиент ниже 6.2 — второе нажатие не раньше 400 мс;
- обрыв сети на `/confirm` — POST не повторяется, опрос GET раз в 2 с, предел 5 мин, пауза на
  скрытой вкладке;
- `actions.enabled=false` из /api/me — «Действия →» и «Поставить стоп →» ведут в чат;
- «Поставить стоп →» — сразу редактор стопа.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_miniapp_market import _body, _const

_ROOT = Path(__file__).resolve().parents[1]
_MINIAPP = _ROOT / "miniapp"


def _js(name: str) -> str:
    return (_MINIAPP / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def _template(match: re.Match[str]) -> str:
    # Шаблонная строка: текст выбрасывается, выражения ${…} остаются коду — арифметика
    # внутри подстановки тоже видна.
    return " ".join(f"({expr})" for expr in re.findall(r"\$\{([^}]*)\}", match.group(0)))


def _strip(text: str) -> str:
    """Без комментариев и текста строк: остаётся код, в котором ищется арифметика."""
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"`(?:\\.|\$\{[^}]*\}|[^`\\])*`", _template, text)
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', text)


_ARITH = re.compile(r"[\w)\]]\s*(?:\*|/|%|(?<!\+)\+(?!\+)|(?<![-=])-(?![-=>]))\s*[\w(]")
# Унарный плюс/минус перед значением («+chip» — перевод строки в число).
_UNARY = re.compile(r"[(,:=?!&|]\s*[+-]\s*[\w(]")
# Единственные места с арифметикой — время и пауза опроса, не числа сервера. Каждая строка
# — ровно одна в файле; всё остальное проверяется регуляркой и списком запретов целиком.
_ALLOWED_ARITHMETIC = (
    # пауза опроса экрана после ошибок: 5 → 10 → 20 → 30 с
    "const delay = failures ? Math.min(INFO_POLL_MS * 2 ** failures, MAX_BACKOFF_MS)"
    " : INFO_POLL_MS;",
    "const wait = Math.min(INFO_POLL_MS * 2 ** failures, MAX_BACKOFF_MS) / 1000;",
    # срок карточки: ttl_seconds сервера от ответа, монотонные часы
    "deadline = performance.now() + ttlSeconds * 1000;",
    "return Math.ceil((deadline - performance.now()) / 1000);",
    # предел опроса итога
    "if (!pollUntil) pollUntil = performance.now() + POLL_LIMIT_MS;",
    # порог второго нажатия (клиент ниже 6.2)
    "if (now - armedAt < SECOND_PRESS_MS) return false;",
)
_BANNED_NUMBERS = ("Number(", "parseFloat", "parseInt", "toFixed", "toPrecision", "BigInt",
                   "Number.parse")


# --- числа — только строки сервера ---------------------------------------------------------


def test_no_arithmetic_on_server_numbers() -> None:
    js = _js("actions.js")
    for banned in _BANNED_NUMBERS:
        assert banned not in js, banned
    rest = js
    for line in _ALLOWED_ARITHMETIC:
        assert js.count(line) == 1, line
        rest = rest.replace(line, "")
    code = _strip(rest)
    assert "Math." not in code
    hits = [line.strip() for line in code.split("\n")
            if _ARITH.search(line) or _UNARY.search(line)]
    assert hits == []


def _arith(sample: str) -> bool:
    code = _strip(sample)
    return bool(_ARITH.search(code) or _UNARY.search(code))


def test_arithmetic_check_catches_conversions() -> None:
    """Сама проверка: перевод строки сервера в число или пересчёт — и в шаблонной строке —
    находится (база для мутации «parseFloat на чипе»)."""
    for sample in ("level_percent: chip * 1", "x = `${p.quantity * 2} XRP`",
                   "const q = row.value - fee;", "level_percent: +chip", "f(-p.entry_price)"):
        assert _arith(sample), sample
    for sample in ("label = `${groupDigits(qty)} ${base}`",
                   'row.value.slice(0, -" маркетом".length)',
                   "const x = a ? b : c;", "(event) => event.target"):
        assert not _arith(sample), sample


def test_time_functions_use_only_clock_and_constants() -> None:
    js = _js("actions.js")
    assert _body(js, "function startDeadline(") == (
        "{\n    deadline = performance.now() + ttlSeconds * 1000;\n  }")
    assert _body(js, "function secondsLeft(") == (
        "{\n    return Math.ceil((deadline - performance.now()) / 1000);\n  }")


def test_render_helpers_only_format_server_strings() -> None:
    js = _js("actions.js")
    for name in ("export function editLabel(", "export function cardLabel(",
                 "export function actionQuestion(", "export function actionLines(",
                 "function renderInfo(", "function showRows("):
        body = _body(js, name)
        for banned in ("Number(", "parseFloat", "toFixed", "Math."):
            assert banned not in body, (name, banned)
        assert not _ARITH.search(_strip(body)), name
    diff = _body(_js("render.js"), "export function actionDiff(")
    assert "before.get(r.label) !== r.value" in diff
    assert "Number(" not in diff and "Math." not in diff and not _ARITH.search(_strip(diff))


def test_stop_chip_goes_to_server_as_string_percent() -> None:
    js = _js("actions.js")
    edit = _body(js, "function editBody(")
    assert 'if (chip === "be") return { ...base, kind: "be" };' in edit
    assert 'if (chip) return { ...base, kind: "sl", level_percent: chip };' in edit
    assert "parseAmount(raw)" in edit and "level };" in edit
    chips = _body(js, 'el.chips.addEventListener("click", (event) => {')
    assert "chip = button.dataset.chip;" in chips
    assert 'export const STOP_CHIPS = ["0.5", "1", "1.5"];' in js
    html = _js("index.html")
    found = re.findall(r'<button type="button" data-chip="([^"]+)">([^<]+)</button>', html)
    # Подписи без знака (решение владельца 25.10), сторону учитывает сервер.
    assert found == [("0.5", "0.5%"), ("1", "1%"), ("1.5", "1.5%"), ("be", "БУ")]


def test_main_button_numbers_are_server_strings() -> None:
    js = _js("actions.js")
    edit = _body(js, "export function editLabel(")
    assert "groupDigits(card.new_level)" in edit
    label = _body(js, "export function cardLabel(")
    assert label.startswith("{\n  if (card.risk_increase) return RISK_TEXT;")
    assert 'export const RISK_TEXT = "⚠️ Да, увеличить риск";' in js
    assert "card.rows.find(" in label and "groupDigits(qty)" in label


# --- превью и таймер карточки --------------------------------------------------------------


def test_preview_has_no_timer() -> None:
    js = _js("actions.js")
    for name in ("function startEdit(", "async function runPreview(", "function schedulePreview(",
                 "function editPressed("):
        body = _body(js, name)
        assert "countdown(" not in body and "startDeadline(" not in body, name
    assert "function schedulePreview(delay = PREVIEW_DEBOUNCE_MS) {" in js
    run = _body(js, "async function runPreview(")
    assert 'api.post("/api/actions/preview", next' in run
    assert "if (seq !== previewSeq || mode !== \"edit\") return;" in run   # последний побеждает
    assert "schedulePreview(PREVIEW_RETRY_MS);" in run                    # 429


def test_card_timer_from_server_ttl_not_phone_clock() -> None:
    js = _js("actions.js")
    for banned in ("Date.now", "Date.parse", "new Date", "expires_at", "getTime("):
        assert banned not in js, banned
    show = _body(js, "function showCard(res)")
    assert "startDeadline(res.ttl_seconds);" in show
    assert show.index("startDeadline(") < show.index("countdown();")
    assert js.count("startDeadline(") == 2               # определение и карточка
    countdown = _body(js, "function countdown()")
    assert "const left = secondsLeft();" in countdown and "expired(EXPIRED_TEXT);" in countdown
    assert 'setAction("Пересчитать", recalc);' in _body(js, "function expired(message)")
    assert "createCard(body, preview);" in _body(js, "function recalc()")


def test_expired_card_never_confirms() -> None:
    pressed = _body(_js("actions.js"), "async function confirmPressed()")
    post = pressed.index("api.post(")
    checks = [m.start() for m in re.finditer(r"if \(secondsLeft\(\) <= 0\) \{", pressed)]
    assert len(checks) == 2 and checks[0] < post and checks[1] < post
    # До вопроса — для любого пути, после вопроса — только в ветке showConfirm.
    assert checks[0] < pressed.index("const needAsk")


# --- подтверждение -------------------------------------------------------------------------


def test_demo_two_presses_show_confirm_only_live_or_risk() -> None:
    js = _js("actions.js")
    pressed = _body(js, "async function confirmPressed()")
    assert "const needAsk = live() || Boolean(card.risk_increase);" in pressed
    branch = _body(pressed, "if (needAsk) {")
    assert "await ask(question)" in branch and "secondPress(question)" in branch
    outside = pressed.replace(branch, "")
    assert "ask(" not in outside and "secondPress(" not in outside
    assert 'const popup = tg && tg.isVersionAtLeast("6.2");' in js
    assert "tg.showConfirm(question" in _body(js, "function ask(question)")
    assert re.search(r"window\.confirm\(|alert\(", js) is None
    # Прямое действие: кнопка → карточка (POST /api/actions), «Да» — только с MainButton карточки.
    direct = _body(js, "function direct(nextCode)")
    assert "createCard(" in direct and "confirm" not in direct
    # confirmPressed — главной кнопкой только у карточки (showCard и cardAgain).
    assert len(re.findall(r"confirmPressed\b(?!\()", js.replace("async function confirmPressed()",
                                                                ""))) == 3
    assert "setAction(cardLabel(code, card, target.symbol), confirmPressed);" in _body(
        js, "function showCard(res)")


def test_risk_confirmed_equals_card_risk_increase() -> None:
    pressed = _body(_js("actions.js"), "async function confirmPressed()")
    assert ("api.post(`/api/actions/${actionId}/confirm`,\n"
            "        { risk_confirmed: Boolean(card.risk_increase) });") in pressed
    assert pressed.count("risk_confirmed") == 1


def test_question_live_first_line() -> None:
    question = _body(_js("actions.js"), "export function actionQuestion(")
    assert "if (live) lines.unshift(LIVE_TEXT);" in question
    assert 'r.label === "Риск увеличится"' in question


def test_second_press_threshold_on_old_clients() -> None:
    js = _js("actions.js")
    second = _body(js, "function secondPress(question)")
    assert "await" not in second
    assert "if (now - armedAt < SECOND_PRESS_MS) return false;" in second
    assert second.index("armedAt = now;") > second.index("if (armed) {")
    assert ('import { BUSY_RETRY_MS, NOT_DELIVERED_TEXT, SECOND_PRESS_MS } from "./confirm.js";'
            in js)
    assert "export const SECOND_PRESS_MS = 400;" in _js("confirm.js")
    pressed = _body(js, "async function confirmPressed()")
    old = pressed[pressed.index("if (!popup) {"):pressed.index("} else {")]
    assert "await" not in old and "if (!secondPress(question)) return;" in old


# --- обрыв сети и опрос --------------------------------------------------------------------


def test_network_failure_never_reposts_only_polls() -> None:
    js = _js("actions.js")
    pressed = _body(js, "async function confirmPressed()")
    failure = _body(pressed, "if (err instanceof ApiError && err.status === 0) {")
    assert "checking({ afterFailure: true });" in failure
    assert "api.post" not in failure and "confirmPressed" not in failure
    assert "createCard" not in failure
    poll = _body(js, "async function poll()")
    assert "api.get(`/api/actions/${actionId}`" in poll and "api.post" not in poll
    branch = _body(poll, 'if (view && view.status === "CARD" && pollFailure) {')
    assert "cardAgain(NOT_DELIVERED_TEXT);" in branch
    # Вся отправка — только эти четыре места.
    posts = re.findall(r"api\.post\(([^,)]+)", js)
    assert posts == ['"/api/actions/preview"', '"/api/actions"',
                     "`/api/actions/${actionId}/confirm`", "`/api/actions/${id}/decline`"]


def test_poll_every_2s_limit_5min_paused_when_hidden() -> None:
    js = _js("actions.js")
    assert _const(js, "POLL_MS") == 2000
    assert _const(js, "POLL_LIMIT_MS") == 300000
    assert "setInterval" not in js
    checking = _body(js, "function checking({ afterFailure = false } = {}) {")
    assert "if (!pollUntil) pollUntil = performance.now() + POLL_LIMIT_MS;" in checking
    poll = _body(js, "async function poll()")
    assert "if (performance.now() >= pollUntil) {" in poll
    assert "Итог придёт в чат — проверь «Позиции»." in _body(poll,
                                                            "if (performance.now() >= pollUntil) {")
    assert "if (document.hidden) return;" in poll
    assert "pollTimer = setTimeout(poll, POLL_MS);" in poll
    visibility = _body(js, "function onVisibility()")
    assert "clearTimeout(pollTimer)" in visibility
    assert 'if (mode === "checking" && pollTimer === null && controller === null) poll();' \
        in visibility


def test_busy_retries_after_pause_and_back_blocked_while_sending() -> None:
    js = _js("actions.js")
    again = _body(js, "function cardAgain(message, { busy = false } = {}) {")
    assert "BUSY_RETRY_MS" in again and "{ progress: busy }" in again
    assert 'busy: () => active && (mode === "sending" || mode === "checking"),' in js
    assert "backAllowed(false);" in _body(js, "async function confirmPressed()")
    assert "backAllowed(false);" in _body(js, "function checking({ afterFailure = false } = {}) {")


# --- выключатель и вход с «Позиций» ----------------------------------------------------------


def test_disabled_actions_lead_to_chat() -> None:
    js = _js("positions.js")
    enabled = _body(js, "export function actionsEnabled(")
    assert enabled == "{\n  return Boolean(meData && meData.actions && meData.actions.enabled);\n}"
    render = _body(js, "function render(")
    assert "const inApp = actionsEnabled(me());" in render
    act = _body(js, "function actButton(")
    assert "if (inApp) button.dataset.act = editor || \"\";" in act
    assert "else button.dataset.chat = link;" in act
    card = _body(js, "function card(")
    assert "chatLink(username, p.start_param)" in card and "if (inApp || link) {" in card
    assert 'if (!p.stops.length) item.append(actButton("Поставить стоп →", link, "sl", inApp));' \
        in card
    assert 'item.append(actButton("Действия →", link, null, inApp));' in card
    # 403 выключателя на самом экране — тоже в чат.
    actions = _js("actions.js")
    load = _body(actions, "async function loadInfo()")
    assert "{ retry: !disabled(err), chat: disabled(err) }" in load
    assert "err.status === 403" in _body(actions, "function disabled(err)")


def test_set_stop_opens_stop_editor_at_once() -> None:
    positions = _js("positions.js")
    click = _body(positions, 'el.list.addEventListener("click", (event) => {')
    assert "onActions({ symbol, side, editor: act.dataset.act || null," in click
    actions = _js("actions.js")
    enter = _body(actions, "enter() {")
    assert 'if (target.editor === "sl") startEdit("sl");' in enter
    app = _js("app.js")
    assert "screens.actions.open(target);" in _body(app, "function openActions(")
    assert '"positions", "actions", "error"]' in app
    assert "onActions: openActions" in app and "onBack: back" in app


def test_positions_still_only_read() -> None:
    js = _js("positions.js")
    assert "api.post" not in js


# --- API и разметка ------------------------------------------------------------------------


def test_paths_match_server_routes() -> None:
    routes = (_ROOT / "app" / "api" / "routes.py").read_text(encoding="utf-8")
    for path in ('"/positions/{symbol}/{side}/actions"', '"/actions/preview"', '"/actions"',
                 '"/actions/{action_id}/confirm"', '"/actions/{action_id}/decline"',
                 '"/actions/{action_id}"'):
        assert path in routes, path
    js = _js("actions.js")
    assert "`/api/positions/${encodeURIComponent(target.symbol)}/${target.side}/actions`" in js


def test_markup_has_every_element_the_script_uses() -> None:
    html = _js("index.html")
    ids = set(re.findall(r'\$\("([a-z-]+)"\)', _js("actions.js")))
    assert ids and sorted(i for i in ids if f'id="{i}"' not in html) == []
    codes = re.findall(r'data-code="([a-z0-9]+)"', html)
    assert codes == ["be", "sl", "tp", "c25", "c50", "cf"]
    screen = html[html.index('id="screen-actions"'):html.index('id="screen-sdk"')]
    assert screen.count('class="hand') == 1                      # Caveat ≤ 1 на экран


def test_new_level_dashed_on_chart() -> None:
    lines = _body(_js("actions.js"), "export function actionLines(")
    assert 'kind: code === "tp" ? "new-take" : "new-stop", price: card.new_level' in lines
    chart = _js("chart.js")
    assert 'if (kind === "new-stop") return { color: colors.red, lineStyle: S.Dashed };' in chart
    assert 'if (kind === "new-take") return { color: colors.blue, lineStyle: S.Dashed };' in chart


def test_leave_releases_everything() -> None:
    js = _js("actions.js")
    leave = _body(js, "leave() {")
    for part in ("gen += 1;", "closePanel();", "stopAll();", "tg.MainButton.offClick(pressed);",
                 'removeEventListener("visibilitychange", onVisibility)', "chart.destroy();"):
        assert part in leave, part
    stop = _body(js, "function stopAll()")
    assert "stopInfo();" in stop and "stopTimers();" in stop and "cancelPreview();" in stop
