"""Дизайн Mini App (Д.2, §9а «Дизайн Mini App»): токены обеих тем = таблица плана, цветов
вне токенов нет, контраст текста ≥ 4.5:1, шрифты — только через --sans/--mono/--hand
(Caveat — только .hand), тема — colorScheme Telegram (theme.js), нативные кнопки — через
paintButton. По тексту файлов: JS-раннера нет, поведение — сценарии design_* в Chrome."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_MINIAPP = Path(__file__).resolve().parents[1] / "miniapp"
# autocrlf=true на этой машине — переводы строк к \n до разбора.
_CSS = (_MINIAPP / "style.css").read_text(encoding="utf-8").replace("\r\n", "\n")

# Таблица токенов §9а (решения владельца 18.10).
LIGHT = {
    "--paper": "#f6f6f2", "--grid-h": "#e6e6df", "--grid-v": "#ecece6", "--ink": "#16181d",
    "--muted": "#5c606a", "--rule": "#d9d9d2", "--card": "#ffffff", "--shadow": "#16181d",
    "--red": "#d7362b", "--red-text": "#d43329", "--blue": "#1f4fd8", "--chip-line": "#b9bcc2",
    "--chart-grid": "#ecece6", "--danger-rule": "#f0c9c5", "--danger-shadow": "#d7362b",
}
DARK = {
    "--paper": "#14161a", "--grid-h": "#1d2026", "--grid-v": "#1a1d22", "--ink": "#ece9e1",
    "--muted": "#8d929b", "--rule": "#2a2e36", "--card": "#1b1e24", "--shadow": "#3a3f48",
    "--red": "#ff6b5e", "--red-text": "#ff6b5e", "--blue": "#7aa2ff", "--chip-line": "#3a3f48",
    "--chart-grid": "#262a31", "--danger-rule": "#3a2a2a", "--danger-shadow": "#5a2622",
}

_LIGHT_SEL = ':root,\nhtml[data-theme="light"]'
_DARK_SEL = 'html[data-theme="dark"]'
_MEDIA = "@media (prefers-color-scheme: dark) {\n  html:not([data-theme]) {"


def _block(start: str) -> str:
    """Тело блока правил, который начинается строкой `start` (до первой `}`)."""
    i = _CSS.index(start) + len(start)
    if not start.endswith("{"):
        i = _CSS.index("{", i) + 1
    return _CSS[i:_CSS.index("}", i)]


def _tokens(body: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9a-f]{6});", body))


def _without_token_blocks() -> str:
    text = _CSS
    for start in (_LIGHT_SEL, _DARK_SEL, _MEDIA):
        body = _block(start)
        text = text.replace(body, "")
    return text


def _lum(hex_: str) -> float:
    def lin(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# --- токены --------------------------------------------------------------------------------


def test_light_tokens_match_plan() -> None:
    assert _tokens(_block(_LIGHT_SEL)) == LIGHT


def test_dark_tokens_match_plan() -> None:
    assert _tokens(_block(_DARK_SEL)) == DARK


def test_system_dark_fallback_equals_dark_set() -> None:
    """До theme.js (и на экране boot.js без SDK) — системная тема тем же тёмным набором."""
    assert _tokens(_block(_MEDIA)) == DARK


def test_no_colors_outside_token_blocks() -> None:
    rest = _without_token_blocks()
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b", rest) == []
    assert re.findall(r"\b(?:rgba?|hsla?|color-mix)\(", rest) == []
    named = re.findall(r"(?:color|background|border[a-z-]*):\s*(white|black|red|blue|green)\b",
                       rest)
    assert named == []


def test_own_js_has_no_color_literals() -> None:
    for js in sorted(_MINIAPP.glob("*.js")):
        text = js.read_text(encoding="utf-8")
        assert re.findall(r"[\"'`]#[0-9a-fA-F]{3,8}[\"'`]", text) == [], js.name


@pytest.mark.parametrize(("theme", "tokens"), [("light", LIGHT), ("dark", DARK)])
def test_text_contrast_at_least_aa(theme: str, tokens: dict[str, str]) -> None:
    """Текст ≥ 4.5:1 и на бумаге, и на карточке; текст на заливке (главная кнопка, LIVE,
    «Шорт» в сегменте) — тоже. Яркий --red — только линии и рамки."""
    for bg in ("--paper", "--card"):
        for fg in ("--ink", "--muted", "--red-text", "--blue"):
            assert _ratio(tokens[fg], tokens[bg]) >= 4.5, (theme, fg, bg)
    assert _ratio(tokens["--paper"], tokens["--ink"]) >= 4.5, theme
    assert _ratio(tokens["--paper"], tokens["--red-text"]) >= 4.5, theme


def test_bright_red_is_not_used_for_text() -> None:
    rest = _without_token_blocks()
    assert re.findall(r"(?<![-\w])color:\s*var\(--red\)", rest) == []


# --- шрифты --------------------------------------------------------------------------------


def test_font_families_only_through_tokens() -> None:
    families = re.findall(r"font-family:\s*([^;]+);", _CSS)
    assert families and set(families) <= {"var(--sans)", "var(--mono)", "var(--hand)"}
    shorthands = re.findall(r"(?<![-\w])font:\s*([^;]+);", _CSS)
    assert all(v == "inherit" or v.endswith("var(--sans)") for v in shorthands), shorthands


def test_caveat_only_in_hand() -> None:
    css = re.sub(r"/\*.*?\*/", "", _CSS, flags=re.DOTALL)
    assert css.count('"Caveat"') == 1                        # только в --hand
    users = re.findall(r"([^{}]+)\{[^}]*var\(--hand\)", css)
    assert [u.strip() for u in users] == [".hand"]


# --- тема и кнопки -------------------------------------------------------------------------


def test_theme_follows_color_scheme_and_paints_telegram_chrome() -> None:
    text = (_MINIAPP / "theme.js").read_text(encoding="utf-8")
    assert 'tg.colorScheme === "dark"' in text
    assert 'dataset.theme = scheme()' in text
    for version, call in (("6.1", "setBackgroundColor(paper)"),
                          ("6.9", "setHeaderColor(paper)"),
                          ("7.10", "setBottomBarColor(paper)")):
        assert f'if (tg.isVersionAtLeast("{version}")) tg.{call};' in text, call
    assert 'tg.onEvent("themeChanged", applyTheme)' in text
    assert "THEME_EVENT" in text


def test_theme_starts_before_anything_else() -> None:
    app = (_MINIAPP / "app.js").read_text(encoding="utf-8")
    init = app[app.index("function init() {"):]
    assert init.index("initTheme(tg);") < init.index("maBoot.failed()")


def test_page_colors_never_from_theme_params() -> None:
    """Решение 18.10: themeParams задают только выбор набора (colorScheme), не цвета."""
    for js in sorted(_MINIAPP.glob("*.js")):
        text = js.read_text(encoding="utf-8")
        uses = re.findall(r"themeParams\.(\w+)", text)
        assert set(uses) <= ({"bg_color"} if js.name == "theme.js" else set()), (js.name, uses)


def test_native_button_colors_only_through_paint_button() -> None:
    for js in sorted(_MINIAPP.glob("*.js")):
        if js.name == "theme.js":
            continue
        text = js.read_text(encoding="utf-8")
        for call in re.findall(r"(?:MainButton|SecondaryButton)\.setParams\(([^;]*?)\);", text,
                               flags=re.DOTALL):
            assert "color" not in call, (js.name, call)


def test_chart_colors_from_tokens_and_theme_event() -> None:
    text = (_MINIAPP / "chart.js").read_text(encoding="utf-8")
    assert "document.addEventListener(THEME_EVENT, applyTheme)" in text
    assert "document.removeEventListener(THEME_EVENT, applyTheme)" in text
    assert "themeChanged" not in text and "matchMedia" not in text
    assert "upColor: colors.ink, downColor: colors.red" in text


def test_group_digits_is_display_only() -> None:
    text = (_MINIAPP / "render.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    body = text[text.index("export function groupDigits"):]
    body = body[:body.index("\n}\n")]
    assert "Number(" not in body and "parseFloat" not in body and "toFixed" not in body
    assert "/g, NBSP)" in body
    assert "const NBSP = String.fromCharCode(160);" in text     # неразрывный, без символа в коде
    assert " " not in text


# --- Д.3: экран рынка ----------------------------------------------------------------------


def _app() -> str:
    return (_MINIAPP / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_number_font_loads_after_me_before_market() -> None:
    """Plex — после успешного /api/me и до экрана рынка (сдвиг цены ~12 px при подмене,
    design_font_swap); preload нет — на экранах ошибок шрифт не нужен."""
    app = _app()
    start = app[app.index("async function start() {"):]
    i_me = start.index('me = await api.get("/api/me");')
    i_load = start.index("loadNumberFont();")
    i_market = start.index('show("market");')
    assert i_me < i_load < i_market
    assert "document.fonts.load(spec" in app
    html = (_MINIAPP / "index.html").read_text(encoding="utf-8")
    assert "ibm-plex-mono" not in html


def test_plate_has_no_flask_and_groups_equity() -> None:
    """Решение 18.10: 🧪 в плашке нет; equity — с разрядами только в показе."""
    app = _app()
    render = app[app.index("function renderAccount(me) {"):]
    render = render[:render.index("\n}\n")]
    assert "🧪" not in render and "🧪" not in (_MINIAPP / "index.html").read_text(encoding="utf-8")
    assert "groupDigits(me.equity)" in render
    assert '"да — на биржу не уйдёт"' in render


def test_dry_run_line_under_price_from_me() -> None:
    market = (_MINIAPP / "market.js").read_text(encoding="utf-8")
    assert 'export const DRY_RUN_LINE = "сухой прогон — на биржу не уйдёт ·";' in market
    assert "me().flags.dry_run" in market
    assert "${dry ? DRY_RUN_LINE : \"Цена на\"}" in market


def test_plate_moves_between_market_head_and_topbar() -> None:
    app = _app()
    place = app[app.index("function placeAccount(name) {"):]
    place = place[:place.index("\n}\n")]
    assert 'name === "market" ? $("market-account") : $("topbar")' in place
    assert 'show(name' not in place
    show = app[app.index("function show(name"):]
    assert "placeAccount(name);" in show[:show.index("\n}\n")]


def test_price_and_head_rows_have_fixed_heights() -> None:
    """Подмена шрифта не сдвигает экран по вертикали: высоты строк заданы в px."""
    fixed = ((".price {", "line-height: 48px;"), (".symbol-line {", "line-height: 28px;"),
             (".symbol-sub {", "line-height: 16px;"), (".price-time {", "line-height: 16px;"),
             (".market-head {", "min-height: 48px;"))
    for selector, prop in fixed:
        body = _block(selector)
        assert prop in body, (selector, prop)


def test_narrow_screen_shrinks_head_not_clips() -> None:
    """320 px (iPhone SE): монета, ☆ и плашка — в одну строку; монета меньше, плашка уже."""
    i = _CSS.index("@media (max-width: 359px) {\n  .market-head")
    body = _CSS[i:_CSS.index("\n}\n", i)]
    rules = (".symbol-line { font-size: 18px;", ".plate { padding: 0 6px;",
             ".market-head { gap: 4px; }")
    for rule in rules:
        assert rule in body, rule


def test_number_font_never_blocks_market() -> None:
    """Владелец 18.10: загрузка Plex не ждётся перед рынком, отказ шрифта поглощается
    (сценарии design_font_404, design_font_hang)."""
    app = _app()
    assert "await loadNumberFont" not in app
    body = app[app.index("function loadNumberFont() {"):]
    body = body[:body.index("\n}\n")]
    assert ".catch(() => {})" in body and "await" not in body


# --- Д.4: форма сделки ---------------------------------------------------------------------


def _render_fn(name: str) -> str:
    text = (_MINIAPP / "render.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    body = text[text.index(name):]
    return body[:body.index("\n}\n")]


@pytest.mark.parametrize("name", ["export function openAction(", "export function renderPreview(",
                                  "export function riskHint(", "function usd(",
                                  "export function openingFee("])
def test_receipt_and_button_only_format_server_strings(name: str) -> None:
    """Владелец 18.10 (Д.4): числа квитанции и «N XRP» на кнопке — из ответа сервера,
    фронт только форматирует (groupDigits); никакого пересчёта в JS."""
    body = _render_fn(name)
    for banned in ("Number(", "parseFloat", "parseInt", "toFixed", "Math.", "BigInt"):
        assert banned not in body, (name, banned)
    # Арифметика над значениями сервера (c.x * …, calc.y - …).
    assert re.search(r"\b(?:c|calc|card\.calc)\.\w+\s*[-+*/%]\s*[\w(]", body) is None, name


def test_button_and_receipt_numbers_go_through_group_digits() -> None:
    assert "groupDigits(card.calc.quantity)" in _render_fn("export function openAction(")
    preview = _render_fn("export function renderPreview(")
    for value in ("groupDigits(c.quantity)", "usd(c.notional)", "usd(c.margin)", "usd(c.risk_usd)",
                  "groupDigits(c.liq_estimate)", "openingFee(c)"):
        assert value in preview, value
    assert "groupDigits(value)" in _render_fn("function usd(")
    assert "usd(value)" in _render_fn("export function openingFee(")
    assert "openingFee(c)" in _render_fn("export function recordRows(")


def test_form_has_one_hand_note_and_receipt() -> None:
    html = (_MINIAPP / "index.html").read_text(encoding="utf-8")
    trade = html[html.index('id="screen-trade"'):html.index('id="screen-confirm"')]
    assert trade.count("hand") == 1 and 'class="field-hint hand" id="trade-risk-hint"' in trade
    assert 'class="trade-result receipt card"' in trade and ">РАСЧЁТ<" in trade


# --- Д.6: экран позиций --------------------------------------------------------------------


def _positions_js() -> str:
    return (_MINIAPP / "positions.js").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_pnl_text_and_color_from_server_sign() -> None:
    """Владелец 18.10: плюс — синий, минус — красный, ноль — muted; знак и текст — сервера."""
    js = _positions_js()
    assert "pnlClass(p.pnl_sign)" in js and "p.pnl_text ??" in js
    assert "unrealized_pnl" not in js
    cls = js[js.index("export function pnlClass(sign) {"):]
    cls = cls[:cls.index("\n}\n")]
    assert 'if (sign === 1) return "up";' in cls and 'if (sign === -1) return "down";' in cls
    market = (_MINIAPP / "market.js").read_text(encoding="utf-8")
    assert 'p.pnl_text ?? "—"' in market and "unrealized_pnl" not in market
    assert ".position-pnl.flat { color: var(--muted); }" in _CSS


def test_bar_front_only_sets_server_fractions() -> None:
    """Полоска: доли 0..1 и подписи — сервера; фронт только ставит --at (CSS calc)."""
    js = _positions_js()
    bar = js[js.index("export function positionBar(bar) {"):]
    bar = bar[:bar.index("\n}\n")]
    for banned in ("Number(", "parseFloat", "toFixed", "Math.", "* 100", "/ "):
        assert banned not in bar, banned
    for at in ('risk.style.setProperty("--at", String(bar.entry_at));',
               'entry.style.setProperty("--at", String(bar.entry_at));',
               'mark.style.setProperty("--at", String(bar.mark_at));'):
        assert at in bar
    assert "if (!bar) return null;" in bar
    assert "left: calc(var(--at) * 100%);" in _CSS and "width: calc(var(--at) * 100%);" in _CSS


def test_position_without_stop_is_danger_card() -> None:
    js = _positions_js()
    assert 'item.className = `position-card${p.stops.length ? "" : " danger"}`;' in js
    body = _block(".position-card.danger {")
    assert "border-color: var(--red);" in body and "var(--danger-shadow)" in body
