"""Действия с позицией (этап 4, app/execution/position_actions.py): формула
безубытка, риск в $ и R, отказы, потолок риска, текст карточки. Без сети и БД.
Числа — позиция разведки 02.10: XRP-USDT LONG, вход 1.5253."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app.exchanges.base import Position, SymbolInfo
from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.position_actions import (
    ActionInputs,
    ActionPlan,
    breakeven_price,
    loss_at,
    plan_action,
    render_card,
)
from app.execution.position_view import ProtectiveOrder
from app.trading.enums import PositionActionKind as Kind
from app.trading.enums import TradeSide

D = Decimal
F = D("0.0005")
NOW = datetime(2026, 10, 2, tzinfo=UTC)


def _position(side: TradeSide = TradeSide.LONG, qty: str = "30", entry: str = "1.5253") -> Position:
    return Position(
        symbol="XRP-USDT", side=side, quantity=D(qty), entry_price=D(entry),
        mark_price=D("1.56"), leverage=20, unrealized_pnl=D("1"),
        liquidation_price=D("1.4553"), position_id="2105907655281221634",
    )


def _order(price: str, oid: str = "1") -> ProtectiveOrder:
    return ProtectiveOrder(oid, D(price), True, None, "MARK_PRICE")


def _inputs(**overrides: object) -> ActionInputs:
    fields: dict[str, object] = dict(
        position=_position(), stops=(_order("1.4795", "stop-1"),), takes=(),
        mark=D("1.56"), symbol_info=SymbolInfo("XRP-USDT", 4, 0, D(2), D(2)),
        fee_rate=F, min_distance_percent=D("0.1"), entry_fee_per_unit=None,
        one_r_per_unit=D("0.0458"), equity=D(10000), risk_cap_percent=D(2),
    )
    fields.update(overrides)
    return ActionInputs(**fields)  # type: ignore[arg-type]


def _plan(kind: Kind, params: dict, **overrides: object) -> ActionPlan:
    plan = plan_action(kind, params, _inputs(**overrides))
    assert isinstance(plan, ActionPlan), plan
    return plan


def _refusal(kind: Kind, params: dict, **overrides: object) -> ExecutionRefusal:
    refusal = plan_action(kind, params, _inputs(**overrides))
    assert isinstance(refusal, ExecutionRefusal), refusal
    return refusal


# --- безубыток ---------------------------------------------------------------


class TestBreakeven:
    def test_long_formula_and_round_up(self) -> None:
        # (100 + 0.05) / (1 − 0.0005) = 100.10005… → вверх 100.1001
        assert breakeven_price(TradeSide.LONG, D(100), F, D("0.05"), 4) == D("100.1001")

    def test_short_formula_and_round_down(self) -> None:
        # (100 − 0.05) / (1 + 0.0005) = 99.90005… → вниз 99.9000
        assert breakeven_price(TradeSide.SHORT, D(100), F, D("0.05"), 4) == D("99.9000")

    def test_rate_estimate_equals_textbook_formula(self) -> None:
        e = D("1.5253")
        exact = (e * (1 + F) / (1 - F))
        assert breakeven_price(TradeSide.LONG, e, F, e * F, 4) == exact.quantize(
            D("0.0001"), rounding="ROUND_CEILING"
        )

    def test_zero_fee_is_entry(self) -> None:
        assert breakeven_price(TradeSide.LONG, D("1.5253"), D(0), D(0), 4) == D("1.5253")
        assert breakeven_price(TradeSide.SHORT, D("1.5253"), D(0), D(0), 4) == D("1.5253")

    @pytest.mark.parametrize("side", [TradeSide.LONG, TradeSide.SHORT])
    def test_net_is_zero_or_slightly_positive(self, side: TradeSide) -> None:
        """Обратный счёт: выход по X с комиссиями — нетто ≥ 0 и меньше шага цены × q."""
        e, q, fin = D("1.5253"), D(30), D("0.000763")
        x = breakeven_price(side, e, F, fin, 4)
        sign = 1 if side is TradeSide.LONG else -1
        net = (x - e) * q * sign - fin * q - F * x * q
        assert D(0) <= net < D("0.0001") * q

    def test_actual_entry_fee_moves_level(self) -> None:
        cheap = breakeven_price(TradeSide.LONG, D(100), F, D("0.01"), 4)
        dear = breakeven_price(TradeSide.LONG, D(100), F, D("0.10"), 4)
        assert cheap < dear


# --- перенос стопа -------------------------------------------------------------


class TestMoveStop:
    def test_breakeven_plan(self) -> None:
        plan = _plan(Kind.MOVE_STOP, {"breakeven": True})
        e = D("1.5253")
        assert plan.new_level == breakeven_price(TradeSide.LONG, e, F, e * F, 4)
        assert plan.replaces_order_id == "stop-1"
        assert plan.current_stop == D("1.4795")
        assert plan.risk_after is not None and plan.risk_after <= 0   # ≈0 нетто
        assert not plan.risk_increase
        assert plan.params == {"breakeven": True}

    def test_breakeven_not_reached(self) -> None:
        refusal = _refusal(Kind.MOVE_STOP, {"breakeven": True}, mark=D("1.5265"))
        assert refusal.code is Code.BREAKEVEN_NOT_REACHED

    def test_stop_wrong_side_long_and_short(self) -> None:
        assert _refusal(Kind.MOVE_STOP, {"level": "1.5595"}).code is Code.STOP_WRONG_SIDE
        short = _position(TradeSide.SHORT, entry="1.60")
        assert _refusal(
            Kind.MOVE_STOP, {"level": "1.5605"}, position=short, stops=()
        ).code is Code.STOP_WRONG_SIDE

    def test_custom_level_rounded_toward_position(self) -> None:
        assert _plan(Kind.MOVE_STOP, {"level": "1.50001"}).new_level == D("1.5001")
        short = _position(TradeSide.SHORT, entry="1.60")
        plan = _plan(Kind.MOVE_STOP, {"level": "1.60009"}, position=short, stops=())
        assert plan.new_level == D("1.6000")

    def test_comma_and_bad_input(self) -> None:
        assert _plan(Kind.MOVE_STOP, {"level": "1,50"}).new_level == D("1.5")
        for bad in ("abc", "-1", "0", ""):
            assert _refusal(Kind.MOVE_STOP, {"level": bad}).code is Code.INVALID_PRICE

    def test_ladder_refused(self) -> None:
        stops = (_order("1.47", "a"), _order("1.46", "b"))
        refusal = _refusal(Kind.MOVE_STOP, {"breakeven": True}, stops=stops)
        assert refusal.code is Code.STOP_AMBIGUOUS

    def test_tighter_stop_lowers_risk(self) -> None:
        plan = _plan(Kind.MOVE_STOP, {"level": "1.50"})
        assert not plan.risk_increase
        assert plan.risk_after < plan.risk_before  # type: ignore[operator]

    def test_wider_stop_is_risk_increase_within_cap(self) -> None:
        plan = _plan(Kind.MOVE_STOP, {"level": "1.40"})
        assert plan.risk_increase
        assert plan.r_after > plan.r_before  # type: ignore[operator]

    def test_cap_exceeded_even_with_confirmation(self) -> None:
        refusal = _refusal(Kind.MOVE_STOP, {"level": "1.40"}, equity=D(100), risk_cap_percent=D(2))
        assert refusal.code is Code.RISK_CAP_EXCEEDED

    @pytest.mark.parametrize("overrides", [{"equity": None}, {"risk_cap_percent": None}])
    def test_cap_unknown_refuses_increase(self, overrides: dict) -> None:
        refusal = _refusal(Kind.MOVE_STOP, {"level": "1.40"}, **overrides)
        assert refusal.code is Code.RISK_CAP_UNKNOWN

    def test_no_stop_yet_is_not_increase(self) -> None:
        plan = _plan(Kind.MOVE_STOP, {"level": "1.40"}, stops=())
        assert not plan.risk_increase and plan.risk_before is None
        assert plan.replaces_order_id is None

    def test_loss_includes_both_fees(self) -> None:
        inputs = _inputs()
        loss = loss_at(inputs, D("1.4795"), D(30))
        e = D("1.5253")
        assert loss == (e - D("1.4795")) * 30 + D("1.4795") * 30 * F + e * F * 30


# --- тейк ------------------------------------------------------------------------


class TestSetTake:
    def test_plan(self) -> None:
        plan = _plan(Kind.SET_TAKE, {"level": "1.62"})
        assert plan.new_level == D("1.62") and plan.replaces_order_id is None
        assert plan.pnl_estimate is not None and plan.pnl_estimate > 0
        assert plan.risk_after == plan.risk_before and not plan.risk_increase

    def test_replaces_single_take(self) -> None:
        plan = _plan(Kind.SET_TAKE, {"level": "1.62"}, takes=(_order("1.60", "tp-1"),))
        assert plan.replaces_order_id == "tp-1" and plan.current_take == D("1.60")

    def test_wrong_side_and_ladder(self) -> None:
        assert _refusal(Kind.SET_TAKE, {"level": "1.5601"}).code is Code.TAKE_WRONG_SIDE
        takes = (_order("1.6", "a"), _order("1.7", "b"))
        assert _refusal(Kind.SET_TAKE, {"level": "1.65"}, takes=takes).code is Code.TAKE_AMBIGUOUS


# --- закрытие --------------------------------------------------------------------


class TestClose:
    def test_partial_25_rounds_down_to_lot(self) -> None:
        plan = _plan(Kind.CLOSE_PARTIAL, {"fraction": "25"})
        assert plan.close_qty == D(7) and plan.remainder == D(23)   # 30 × 0.25 = 7.5 → 7
        assert plan.fee == D(7) * D("1.56") * F
        assert plan.risk_after < plan.risk_before  # type: ignore[operator]

    def test_partial_50(self) -> None:
        assert _plan(Kind.CLOSE_PARTIAL, {"fraction": "50"}).close_qty == D(15)

    def test_too_small_and_remainder(self) -> None:
        tiny = _position(qty="4")
        assert _refusal(Kind.CLOSE_PARTIAL, {"fraction": "25"}, position=tiny).code is (
            Code.CLOSE_TOO_SMALL
        )
        # Минимальный нотионал: 7 × 1.56 = 10.92 < 20.
        rich_min = SymbolInfo("XRP-USDT", 4, 0, D(2), D(20))
        refusal = _refusal(Kind.CLOSE_PARTIAL, {"fraction": "25"}, symbol_info=rich_min)
        assert refusal.code is Code.CLOSE_TOO_SMALL and "+" not in refusal.message
        assert _refusal(Kind.CLOSE_PARTIAL, {"fraction": "33"}).code is Code.INVALID_PRICE

    def test_full_close_pnl_and_zero_risk(self) -> None:
        plan = _plan(Kind.CLOSE_FULL, {})
        e, m, q = D("1.5253"), D("1.56"), D(30)
        assert plan.close_qty == q and plan.remainder == 0 and plan.risk_after == 0
        assert plan.pnl_estimate == (m - e) * q - q * m * F - e * F * q


# --- карточка --------------------------------------------------------------------


class TestRenderCard:
    def test_breakeven_card(self) -> None:
        inputs = _inputs()
        plan = plan_action(Kind.MOVE_STOP, {"breakeven": True}, inputs)
        assert isinstance(plan, ActionPlan)
        text = render_card(plan, inputs)
        assert "XRP-USDT LONG · стоп в безубыток" in text
        assert "Стоп: 1.4795 → <b>" in text and "(на всю позицию)" in text
        assert "≈0 нетто без учёта проскальзывания" in text
        assert "не торговая рекомендация" in text

    def test_risk_increase_is_flagged(self) -> None:
        inputs = _inputs()
        plan = plan_action(Kind.MOVE_STOP, {"level": "1.40"}, inputs)
        assert isinstance(plan, ActionPlan)
        assert "⚠️ <b>Риск увеличится:</b>" in render_card(plan, inputs)

    def test_partial_close_card_keeps_stop_note(self) -> None:
        inputs = _inputs()
        plan = plan_action(Kind.CLOSE_PARTIAL, {"fraction": "25"}, inputs)
        assert isinstance(plan, ActionPlan)
        text = render_card(plan, inputs)
        assert "Закрыть 25%: <b>7</b> маркетом" in text
        # 08.10.2026: тейка нет — так и пишем (раньше «стоп и тейк остаются»)
        assert "Остаток 23: стоп на всю позицию; тейка нет" in text

    def test_partial_close_both_close_position(self) -> None:
        inputs = _inputs(takes=(_order("1.62", "tp-1"),))
        plan = plan_action(Kind.CLOSE_PARTIAL, {"fraction": "25"}, inputs)
        assert isinstance(plan, ActionPlan)
        assert "Остаток 23 — стоп и тейк остаются на всю позицию" in render_card(plan, inputs)

    def test_partial_close_sized_orders_shrink(self) -> None:
        """Стоп и тейк на объём (вложенные / «на часть позиции», «В журнал»):
        биржа уменьшает их под остаток сама — проверено Т0 08.10 (195 → 147)."""
        sized_stop = ProtectiveOrder("stop-1", D("1.4795"), False, D(30), "MARK_PRICE")
        sized_take = ProtectiveOrder("tp-1", D("1.62"), False, D(30), "MARK_PRICE")
        inputs = _inputs(stops=(sized_stop,), takes=(sized_take,))
        plan = plan_action(Kind.CLOSE_PARTIAL, {"fraction": "25"}, inputs)
        assert isinstance(plan, ActionPlan)
        text = render_card(plan, inputs)
        assert (
            "Остаток 23: стоп на 30 — биржа уменьшит до 23; тейк на 30 — биржа уменьшит до 23"
            in text
        )
        assert "на всю позицию" not in text

    def test_r_shown_when_known(self) -> None:
        inputs = _inputs()
        plan = plan_action(Kind.MOVE_STOP, {"level": "1.50"}, inputs)
        assert isinstance(plan, ActionPlan)
        r = (plan.risk_before / (D("0.0458") * 30)).quantize(D("0.01"), ROUND_HALF_UP)  # type: ignore[operator]
        assert f"({r}R)" in render_card(plan, inputs)


def test_action_client_order_ids_unique_case_insensitive() -> None:
    """BingX хранит clientOrderId в нижнем регистре (docs-v3, живьём 02.10:
    tm11u1T → tm11u1t) — наши ключи обязаны различаться и без регистра."""
    from app.execution.models import action_client_order_id
    from app.trading.enums import OrderRole

    ids = [
        action_client_order_id(action_id=a, user_id=u, role=role, bridge=bridge)
        for a in range(1, 120) for u in range(1, 15)
        for role in (OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT, OrderRole.CLOSE)
        for bridge in (False, True)
    ]
    assert len({i.casefold() for i in ids}) == len(ids)
    assert all(1 <= len(i) <= 40 for i in ids)
