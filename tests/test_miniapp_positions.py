"""Вкладка «Позиции» и меню «…» Mini App (П.1) — по тексту `miniapp/positions.js`,
`app.js`, `market.js`, `index.html` (JS-раннера нет; поведение в браузере —
`miniapp_browser_check --scenario pos_*|menu_*`):

- только просмотр: positions.js ничего не шлёт кроме GET /api/positions, действий
  этапа 4 нет — кнопка ведёт в чат ссылкой с параметром сервера;
- опрос цепочкой setTimeout не чаще серверного кэша, стоп на скрытой вкладке и уходе;
- меню: SettingsButton (7.0+) → showPopup «Позиции»/«Обновить», без него — своя ссылка.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.api import positions as api_positions
from tests.test_miniapp_market import _body, _const

_MINIAPP = Path(__file__).resolve().parents[1] / "miniapp"


def _js(name: str) -> str:
    return (_MINIAPP / name).read_text(encoding="utf-8")


def test_positions_only_reads() -> None:
    js = _js("positions.js")
    calls = re.findall(r"\bapi\.(\w+)\(\s*\"([^\"]+)\"", js)
    assert calls == [("get", "/api/positions")]
    assert "api.post" not in js and "fetch(" not in js


def test_chat_link_uses_server_param() -> None:
    """Формат pos-… строит сервер (start_param), фронт его не собирает."""
    js = _js("positions.js")
    assert not re.search(r"[\"'`]pos-", js)              # строкой в коде — нет
    link = _body(js, "export function chatLink(")
    assert "username && param ?" in link and "${CHAT_URL}${username}?start=${param}" in link
    assert 'const CHAT_URL = "https://t.me/";' in js
    chat = _body(js, "function openChat(")
    assert "tg.openTelegramLink(link)" in chat and "tg.openLink(link)" in chat
    # Имени бота нет — вместо кнопки подсказка (getMe не прочитан, стенд).
    card = _body(js, "function card(")
    assert "chatLink(username, p.start_param)" in card and "if (inApp || link) {" in card
    hint = card[card.index("if (inApp || link) {"):]
    assert "} else {" in hint and "Действия с позицией — в чате бота: «💼 Позиции»." in hint


def test_poll_is_chained_and_not_faster_than_cache() -> None:
    js = _js("positions.js")
    assert "setInterval" not in js
    assert _const(js, "POLL_MS") >= api_positions.TTL_POSITIONS * 1000
    assert _const(js, "MAX_BACKOFF_MS") == 30000
    tick = _body(js, "async function tick(")
    assert tick.startswith("{\n    timer = null;") and "document.hidden" in tick
    schedule = _body(js, "function schedule(")
    assert "document.hidden" in schedule and "timer !== null" in schedule


def test_leave_stops_everything() -> None:
    js = _js("positions.js")
    leave = _body(js, "leave() {")
    assert "stopPolling();" in leave and "gen += 1;" in leave
    assert 'removeEventListener("visibilitychange", onVisibility)' in leave
    enter = _body(js, "enter() {")
    assert 'addEventListener("visibilitychange", onVisibility)' in enter
    stop = _body(js, "function stopPolling(")
    assert "clearTimeout(timer)" in stop and "controller.abort()" in stop


def test_states_401_and_errors() -> None:
    js = _js("positions.js")
    fatal = _body(js, "function fatal(")
    assert "err.status === 401" in fatal and "onFatal(err)" in fatal
    load = _body(js, "async function load(")
    assert "showState(" in load and "{ retry: true }" in load
    tick = _body(js, "async function tick(")
    assert "el.stale.hidden = false;" in tick and "failures += 1;" in tick
    render = _body(js, "function render(")
    assert "data.exchange_error" in render                        # NO_KEY / KEY_REJECTED
    assert "data.bot_account_mode !== data.account_mode" in render
    assert "el.empty.hidden = data.positions.length > 0 || Boolean(data.exchange_error);" in render


def test_dom_built_without_html_strings() -> None:
    js = _js("positions.js")
    assert "innerHTML" not in js and "textContent" in js


def test_menu_settings_button_popup_and_fallback() -> None:
    app = _js("app.js")
    assert 'tg.isVersionAtLeast("7.0") ? tg.SettingsButton : null' in app
    menu = _body(app, "const MENU = {")
    assert '{ id: "positions", type: "default", text: "💼 Позиции" }' in menu
    assert '{ id: "refresh", type: "default", text: "🔄 Обновить" }' in menu
    assert '{ type: "cancel" }' in menu
    popup = _body(app, "function openMenu(")
    assert "if (busy()) return;" in popup and "tg.showPopup(MENU" in popup
    assert "catch (err)" in popup
    init = _body(app, "function init(")
    assert "settingsButton.onClick(openMenu);" in init and "settingsButton.show();" in init
    assert '$("account-positions")' in init and "link.hidden = false;" in init
    html = (_MINIAPP / "index.html").read_text(encoding="utf-8")
    assert re.search(r'id="account-positions"\s+hidden>💼 Позиции</button>', html)
    assert 'id="screen-positions" hidden' in html


def test_refresh_respects_busy_and_401() -> None:
    app = _js("app.js")
    refresh = _body(app, "async function refresh(")
    assert refresh.index("busy()") < refresh.index('api.get("/api/me")')
    assert "err.status === 401" in refresh and "showError(err, start)" in refresh
    assert "screens[current].refresh()" in refresh
    positions = _body(app, "function openPositions(")
    assert "busy()" in positions and "screens.positions.refresh()" in positions


def test_card_opens_market_of_symbol() -> None:
    app = _js("app.js")
    open_symbol = _body(app, "function openSymbol(")
    assert "screens.market.setSymbol(symbol);" in open_symbol and 'show("market")' in open_symbol
    market = _js("market.js")
    set_symbol = _body(market, "setSymbol(next) {")
    assert 'storage.save("symbol", next);' in set_symbol and "if (active) {" in set_symbol
