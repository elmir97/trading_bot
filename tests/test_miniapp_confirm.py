"""Подтверждение Mini App (подэтап 2.5, план — «да» владельца 17.10) — по тексту
`miniapp/confirm.js`, `render.js`, `app.js`, `index.html` (JS-раннера нет; в браузере —
`miniapp_browser_check --scenario confirm_*` на стенде):

- /confirm — только после showConfirm (старый клиент — второе нажатие), один POST на
  нажатие, accept_warnings — из карточки сервера;
- обрыв сети на /confirm — POST не повторяется сам: опрос GET; CARD — «Не дошло, нажми
  ещё раз» и повтор по тому же id (сервер: test_repeat_confirm_same_id_one_opening);
- LIVE — «⚠️ Реальный счёт» на экране и первой строкой в showConfirm;
- отсчёт — от expires_at сервера, истекла — «Пересчитать» (новый POST /api/open);
- BLOCK/отказ на свежих данных — кнопки нет; пока /confirm в полёте — «Назад» нет;
- поля формы — только в сессии, без localStorage.
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


def test_confirm_posts_once_after_show_confirm() -> None:
    confirm = _js("confirm.js")
    pressed = _body(confirm, "async function confirmPressed()")
    asked = pressed.index("await ask(confirmQuestion(card, body.side, body.symbol, live()))")
    post = pressed.index("api.post(`/api/open/${openingId}/confirm`")
    assert asked < post
    assert pressed.count("api.post(") == 1
    assert "{ accept_warnings: Boolean(card.has_warnings) }" in pressed
    # Повторное нажатие во время отправки ничего не шлёт.
    assert 'if (mode !== "card") return;' in pressed
    assert 'mode = "sending";' in pressed
    # Вся отправка — только из confirmPressed / cancelPressed / create.
    posts = re.findall(r"api\.post\(([^,)]+)", confirm)
    assert posts == ["`/api/open/${openingId}/confirm`", "`/api/open/${openingId}/cancel`",
                     '"/api/open"']


def test_show_confirm_or_second_press_without_window_confirm() -> None:
    confirm = _js("confirm.js")
    assert 'const popup = tg && tg.isVersionAtLeast("6.2");' in confirm
    ask = _body(confirm, "function ask(question)")
    assert "tg.showConfirm(question, (ok) => resolve(Boolean(ok)))" in ask
    assert "if (armed) {" in ask and "armed = true;" in ask
    assert re.search(r"window\.confirm\(|alert\(", confirm) is None


def test_network_failure_polls_and_card_means_not_delivered() -> None:
    confirm = _js("confirm.js")
    pressed = _body(confirm, "async function confirmPressed()")
    failure = _body(pressed, "if (err instanceof ApiError && err.status === 0) {")
    assert "checking({ afterFailure: true });" in failure
    assert "api.post" not in failure                          # сам POST не повторяет
    poll = _body(confirm, "async function poll(afterFailure)")
    assert "api.get(`/api/open/${openingId}`" in poll
    branch = _body(poll, 'if (view && view.status === "CARD" && afterFailure) {')
    assert "status(NOT_DELIVERED_TEXT);" in branch
    assert "setAction(confirmLabel(), confirmPressed);" in branch   # тот же id
    assert 'export const NOT_DELIVERED_TEXT = "Не дошло, нажми ещё раз.";' in confirm
    assert "export const POLL_MS = 2000;" in confirm
    assert "if (Date.now() >= pollUntil) {" in poll


def test_live_marker_on_screen_and_in_question() -> None:
    render = _js("render.js")
    assert 'export const LIVE_TEXT = "⚠️ Реальный счёт";' in render
    question = _body(render, "export function confirmQuestion(")
    assert "return live ? `${LIVE_TEXT}\\n${ask}` : ask;" in question
    assert "${c.quantity}" in question and "риск ${c.risk_usd} $" in question
    assert "return live ? `${LIVE_TEXT}\\n${ask}` : ask;" in _body(render,
                                                                  "export function cancelQuestion(")
    confirm = _js("confirm.js")
    assert 'const live = () => me().account_mode === "LIVE";' in confirm
    assert "el.live.hidden = !live();" in _body(confirm, "async function create()")
    assert "cancelQuestion(opening, live())" in confirm
    html = _js("index.html")
    assert '<p class="live-warning" id="confirm-live" hidden>⚠️ Реальный счёт</p>' in html


def test_block_or_refusal_has_no_open_button() -> None:
    show = _body(_js("confirm.js"), "function showCard(next)")
    branch = _body(show, 'if (!openingId || next.status !== "CARD" || !card.can_open) {')
    assert "setAction(null);" in branch and "return;" in branch
    assert show.index(branch) < show.index("setAction(confirmLabel(), confirmPressed);")


def test_countdown_from_server_and_expired_recalculates() -> None:
    confirm = _js("confirm.js")
    countdown = _body(confirm, "function countdown(expiresAt)")
    assert "Date.parse(expiresAt) - Date.now()" in countdown
    assert "expired(" in countdown
    assert "countdown(next.expires_at);" in _body(confirm, "function showCard(next)")
    assert 'setAction("Пересчитать", recalc);' in _body(confirm, "function expired(message)")
    assert "create();" in _body(confirm, "function recalc()")
    create = _body(confirm, "async function create()")
    assert 'const res = await api.post("/api/open", body);' in create
    assert 'if (view.status === "EXPIRED_CARD") {' in _body(confirm, "{ busy = false } = {}) {")


def test_back_blocked_while_sending() -> None:
    confirm = _js("confirm.js")
    assert 'busy: () => active && (mode === "sending" || mode === "checking"),' in confirm
    assert "backAllowed(false);" in _body(confirm, "async function confirmPressed()")
    back = _body(_js("app.js"), "function back()")
    assert "if (screens[current] && screens[current].busy && screens[current].busy()) return;" \
        in back
    assert back.index(".busy()") < back.index("history.pop()")


def test_result_uses_opening_status_not_reply_final() -> None:
    """Повторный /confirm по открытой сделке: final=false, но открытие итоговое (DONE)."""
    handle = _body(_js("confirm.js"), "{ busy = false } = {}) {")
    assert "} else if (view.final) {" in handle
    assert "res.final" not in _js("confirm.js")
    lines = _body(_js("render.js"), "export function resultLines(")
    assert "`Сделка #${opening.trade_id}`" in lines
    assert "opening.stop_placed" in lines and "opening.take_placed" in lines


def test_limit_working_and_cancel_with_confirm() -> None:
    confirm = _js("confirm.js")
    working = _body(confirm, "function working(view, serverText)")
    assert "⏳ Лимит выставлен" in working and "истекает" in working
    assert 'setAction("Отменить лимит", cancelPressed);' in working
    cancel = _body(confirm, "async function cancelPressed()")
    assert cancel.index("await ask(cancelQuestion(") < cancel.index("api.post(")


def test_fields_live_only_in_session() -> None:
    assert "storage" not in _js("confirm.js") and "localStorage" not in _js("confirm.js")
    assert "storage.save" not in _js("form.js")
    # «Назад к форме» — та же форма: open() (сброс полей) зовётся только с рынка.
    app = _js("app.js")
    assert "screens.trade.open(side, symbol);" in _body(app, "function openTrade(")
    assert app.count("screens.trade.open(") == 1


def test_card_diff_compares_server_strings_only() -> None:
    diff = _body(_js("render.js"), "export function cardDiff(")
    assert "String(preview.calc[key]) !== String(card.calc[key])" in diff
    assert "Number(" not in diff
