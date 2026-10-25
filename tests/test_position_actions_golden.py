"""Эталон текста карточки действия в чате (5.0, 25.10.2026): render_card байт в
байт тот же, что до выноса полей карточки в card_view (Mini App, экран 5).
Строки сняты с кода 32b8248; 5.0а (25.10) — осознанно изменены breakeven (ноль
без знака, «≈0 USDT») и take_new/take_move (без «Риск после»); правка текста — вместе с
эталоном. Без сети и БД."""

from __future__ import annotations

import pytest

from app.execution.position_actions import ActionPlan, plan_action, render_card
from app.trading.enums import PositionActionKind as Kind
from app.trading.enums import TradeSide
from tests.test_position_actions import D, _inputs, _order, _position

CASES: dict[str, tuple[Kind, dict[str, object], dict[str, object]]] = {
    "breakeven": (Kind.MOVE_STOP, {"breakeven": True}, {}),
    "stop_closer": (Kind.MOVE_STOP, {"level": "1.5"}, {}),
    "stop_risk_up": (Kind.MOVE_STOP, {"level": "1.45"}, {}),
    "stop_none": (Kind.MOVE_STOP, {"level": "1.5"}, {"stops": ()}),
    "stop_no_r_no_equity": (
        Kind.MOVE_STOP, {"level": "1.5"}, {"one_r_per_unit": None, "equity": None},
    ),
    "stop_short": (
        Kind.MOVE_STOP, {"level": "1.6"},
        {"position": _position(TradeSide.SHORT, entry="1.58"), "stops": (_order("1.62"),),
         "mark": D("1.55")},
    ),
    "take_new": (Kind.SET_TAKE, {"level": "1.7"}, {}),
    "take_move": (Kind.SET_TAKE, {"level": "1.7"}, {"takes": (_order("1.65", "take-1"),)}),
    "close_25": (Kind.CLOSE_PARTIAL, {"fraction": "25"}, {"position": _position(qty="100")}),
    "close_50_sized": (
        Kind.CLOSE_PARTIAL, {"fraction": "50"},
        {"position": _position(qty="100"), "takes": (_order("1.7", "t"),)},
    ),
    "close_full": (Kind.CLOSE_FULL, {}, {}),
    "close_full_no_stop": (Kind.CLOSE_FULL, {}, {"stops": ()}),
}

GOLDEN_LINES: dict[str, tuple[str, ...]] = {
    'breakeven': (
        '<b>XRP-USDT LONG · стоп в безубыток</b>',
        'Позиция 30 · вход 1.5253 · mark 1.56',
        'Стоп: 1.4795 → <b>1.5269</b> (на всю позицию)',
        'Риск: 1.42 USDT (1.03R) → ≈0 USDT (0.00R)',
        'Комиссия выхода по стопу ≈0.02 USDT (учтена в риске)',
        '≈0 нетто без учёта проскальзывания (стоп исполняется маркетом)',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'stop_closer': (
        '<b>XRP-USDT LONG · перенос стопа</b>',
        'Позиция 30 · вход 1.5253 · mark 1.56',
        'Стоп: 1.4795 → <b>1.5</b> (на всю позицию)',
        'Риск: 1.42 USDT (1.03R) → 0.80 USDT (0.59R)',
        'Комиссия выхода по стопу ≈0.02 USDT (учтена в риске)',
        'Риск после — 0.01% от 10000.00 USDT',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'stop_risk_up': (
        '<b>XRP-USDT LONG · перенос стопа</b>',
        'Позиция 30 · вход 1.5253 · mark 1.56',
        'Стоп: 1.4795 → <b>1.45</b> (на всю позицию)',
        '⚠️ <b>Риск увеличится:</b> 1.42 USDT → 2.30 USDT (+0.65R)',
        'Комиссия выхода по стопу ≈0.02 USDT (учтена в риске)',
        'Риск после — 0.02% от 10000.00 USDT',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'stop_none': (
        '<b>XRP-USDT LONG · перенос стопа</b>',
        'Позиция 30 · вход 1.5253 · mark 1.56',
        'Стоп: нет → <b>1.5</b> (на всю позицию)',
        'Риск: не ограничен (стопа нет) → 0.80 USDT (0.59R)',
        'Комиссия выхода по стопу ≈0.02 USDT (учтена в риске)',
        'Риск после — 0.01% от 10000.00 USDT',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'stop_no_r_no_equity': (
        '<b>XRP-USDT LONG · перенос стопа</b>',
        'Позиция 30 · вход 1.5253 · mark 1.56',
        'Стоп: 1.4795 → <b>1.5</b> (на всю позицию)',
        'Риск: 1.42 USDT → 0.80 USDT',
        'Комиссия выхода по стопу ≈0.02 USDT (учтена в риске)',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'stop_short': (
        '<b>XRP-USDT SHORT · перенос стопа</b>',
        'Позиция 30 · вход 1.58 · mark 1.55',
        'Стоп: 1.62 → <b>1.6</b> (на всю позицию)',
        'Риск: 1.25 USDT (0.91R) → 0.65 USDT (0.47R)',
        'Комиссия выхода по стопу ≈0.02 USDT (учтена в риске)',
        'Риск после — 0.01% от 10000.00 USDT',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'take_new': (
        '<b>XRP-USDT LONG · тейк</b>',
        'Позиция 30 · вход 1.5253 · mark 1.56',
        'Тейк: нет → <b>1.7</b> (на всю позицию)',
        'PnL по тейку ≈ +5.19 USDT (после комиссий)',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'take_move': (
        '<b>XRP-USDT LONG · тейк</b>',
        'Позиция 30 · вход 1.5253 · mark 1.56',
        'Тейк: 1.65 → <b>1.7</b> (на всю позицию)',
        'PnL по тейку ≈ +5.19 USDT (после комиссий)',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'close_25': (
        '<b>XRP-USDT LONG · частичное закрытие</b>',
        'Позиция 100 · вход 1.5253 · mark 1.56',
        'Закрыть 25%: <b>25</b> маркетом · комиссия ≈0.02 USDT',
        'PnL ≈ +0.83 USDT по mark (после комиссий)',
        'Остаток 75: стоп на всю позицию; тейка нет',
        'Риск: 4.73 USDT (1.03R) → 3.55 USDT (1.03R)',
        'Риск после — 0.04% от 10000.00 USDT',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'close_50_sized': (
        '<b>XRP-USDT LONG · частичное закрытие</b>',
        'Позиция 100 · вход 1.5253 · mark 1.56',
        'Закрыть 50%: <b>50</b> маркетом · комиссия ≈0.04 USDT',
        'PnL ≈ +1.66 USDT по mark (после комиссий)',
        'Остаток 50 — стоп и тейк остаются на всю позицию',
        'Риск: 4.73 USDT (1.03R) → 2.37 USDT (1.03R)',
        'Риск после — 0.02% от 10000.00 USDT',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'close_full': (
        '<b>XRP-USDT LONG · закрыть всё</b>',
        'Позиция 30 · вход 1.5253 · mark 1.56',
        'Закрыть всю позицию: <b>30</b> маркетом · комиссия ≈0.02 USDT',
        'PnL ≈ +0.99 USDT по mark (после комиссий)',
        'Риск: 1.42 USDT (1.03R) → ≈0 USDT',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
    'close_full_no_stop': (
        '<b>XRP-USDT LONG · закрыть всё</b>',
        'Позиция 30 · вход 1.5253 · mark 1.56',
        'Закрыть всю позицию: <b>30</b> маркетом · комиссия ≈0.02 USDT',
        'PnL ≈ +0.99 USDT по mark (после комиссий)',
        'Риск: не ограничен (стопа нет) → ≈0 USDT',
        '',
        '<i>ℹ️ Информация о позиции, не торговая рекомендация.</i>',
    ),
}
GOLDEN = {name: "\n".join(lines) for name, lines in GOLDEN_LINES.items()}


@pytest.mark.parametrize("name", list(CASES))
def test_render_card_byte_for_byte(name: str) -> None:
    kind, params, overrides = CASES[name]
    inputs = _inputs(**overrides)
    plan = plan_action(kind, params, inputs)
    assert isinstance(plan, ActionPlan), plan
    assert render_card(plan, inputs) == GOLDEN[name]


def test_every_case_has_golden() -> None:
    assert set(CASES) == set(GOLDEN)
