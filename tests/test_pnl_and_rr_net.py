"""28.09, блоки C и A (часть в БД): сверка PnL с биржей и RR с комиссией
в сводке. Чистая логика, без БД — числа живого SOL #4 (27.09)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.database.models.execution_order import ExecutionOrder
from app.database.models.reconciliation_event import ReconciliationEvent
from app.exchanges.base import Position
from app.exchanges.bingx import _parse_history_order
from app.execution.reconciler import PNL_TOLERANCE_R, decide_trade, pnl_mismatch
from app.trading.enums import (
    ANOMALY_KINDS,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    ReconciliationKind,
    TradeSide,
)
from app.workers.execution_digest import (
    build_stats,
    detect_anomalies,
    render_execution_digest,
)
from tests.bingx_fixtures import live_items
from tests.test_reconciler_logic import SOL_STOP_CHILD, _sol_trade

D = Decimal
SOL_RISK = D("1766.609542")  # риск SOL #4 с карточки — 1R
SOL_FEES = D("166.602903")  # вход 83.78152 + выход 82.821383


class TestPnlMismatch:
    def test_live_sol_within_tolerance(self) -> None:
        """Журнал −2087.121603, биржа −1920.2749 − 166.602903 = −2086.877803:
        разница 0.24 при допуске 17.67."""
        assert pnl_mismatch(
            trade_id=4, symbol="SOL-USDT", journal_pnl=D("-2087.121603"), fees=SOL_FEES,
            exits_realized_pnl=[D("-1920.2749")], risk_amount=SOL_RISK,
        ) is None

    def test_beyond_tolerance_is_anomaly(self) -> None:
        found = pnl_mismatch(
            trade_id=4, symbol="SOL-USDT", journal_pnl=D("-2058.518133"), fees=SOL_FEES,
            exits_realized_pnl=[D("-1920.2749")], risk_amount=SOL_RISK,
        )
        assert found is not None
        assert (found.kind, found.dedup_key, found.trade_id) == (
            ReconciliationKind.PNL_MISMATCH, "pnl:4", 4,
        )
        assert found.detail == (
            "PnL журнала -2058.518133, по бирже -2086.877803 (profit -1920.2749 − "
            "комиссии 166.602903), разница 28.35967 больше 0.01R (17.66609542)"
        )

    def test_boundary_is_inclusive(self) -> None:
        """Ровно 0.01R — ещё не расхождение; чуть больше — уже."""
        exchange = D("-1920.2749") - SOL_FEES
        edge = exchange + SOL_RISK * PNL_TOLERANCE_R
        kw = {
            "trade_id": 4, "symbol": "SOL-USDT", "fees": SOL_FEES,
            "exits_realized_pnl": [D("-1920.2749")], "risk_amount": SOL_RISK,
        }
        assert pnl_mismatch(journal_pnl=edge, **kw) is None  # type: ignore[arg-type]
        assert pnl_mismatch(journal_pnl=edge + D("0.00000001"), **kw) is not None  # type: ignore[arg-type]

    def test_several_exits_are_summed(self) -> None:
        assert pnl_mismatch(
            trade_id=4, symbol="SOL-USDT", journal_pnl=D("-2087.121603"), fees=SOL_FEES,
            exits_realized_pnl=[D("-1000"), D("-920.2749")], risk_amount=SOL_RISK,
        ) is None

    def test_is_anomaly_kind(self) -> None:
        assert ReconciliationKind.PNL_MISMATCH in ANOMALY_KINDS


def _sol_orders():  # type: ignore[no-untyped-def]
    return [_parse_history_order(item) for item in live_items("allOrders SOL")]


class TestExitCarriesProfit:
    def test_full_close_carries_live_profit(self) -> None:
        decision = decide_trade(_sol_trade(), None, _sol_orders())
        assert [(e.order_id, e.realized_pnl) for e in decision.exits] == [
            (SOL_STOP_CHILD, D("-1920.2749")),
        ]

    def test_partial_close_keeps_profit(self) -> None:
        child = next(o for o in _sol_orders() if o.order_id == SOL_STOP_CHILD)
        part = replace(
            child, order_type="MARKET", trigger_order_id=None, order_id="P1",
            executed_qty=D("362.07"), realized_pnl=D("-510.5"),
        )
        remaining = Position(
            symbol="SOL-USDT", side=TradeSide.LONG, quantity=D("1000"),
            entry_price=D("123.021"), mark_price=D("122"), leverage=10,
            unrealized_pnl=D("0"),
        )
        decision = decide_trade(_sol_trade(), remaining, [part])
        assert [(e.kind, e.realized_pnl) for e in decision.exits] == [
            (ReconciliationKind.PARTIAL_CLOSE, D("-510.5")),
        ]


def _entry(**overrides: object) -> ExecutionOrder:
    fields: dict[str, object] = {
        "user_id": 1, "symbol": "SOL-USDT", "side": OrderSide.BUY,
        "position_side": TradeSide.LONG, "order_type": OrderType.MARKET,
        "role": OrderRole.ENTRY, "status": OrderStatus.DRY_RUN, "risk_percent": D("2"),
    }
    fields.update(overrides)
    return ExecutionOrder(**fields)  # type: ignore[arg-type]


class TestDigest:
    def test_average_rr_shows_both_numbers(self) -> None:
        rows = [
            _entry(risk_reward=D("2.42"), risk_reward_net=D("2.12")),
            _entry(risk_reward=D("2.14"), risk_reward_net=D("2.09")),
        ]
        text = render_execution_digest(
            build_stats(rows, target_risk_percent=None), max_price_drift_ratio=D("0.3")
        )
        assert "Средний RR: 2.28 · с комиссией 2.105" in text

    def test_old_rows_without_net_keep_old_line(self) -> None:
        text = render_execution_digest(
            build_stats([_entry(risk_reward=D("2.5"))], target_risk_percent=None),
            max_price_drift_ratio=D("0.3"),
        )
        assert "Средний RR: 2.5\n" in text + "\n"
        assert "с комиссией" not in text

    def test_pnl_mismatch_is_listed_as_anomaly(self) -> None:
        event = ReconciliationEvent(
            user_id=1, symbol="SOL-USDT", kind=ReconciliationKind.PNL_MISMATCH,
            dedup_key="pnl:4", detail="d",
        )
        stats = build_stats([], target_risk_percent=None, reconciler_events=[event])
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert "сверка с биржей: расхождений 1 (PnL не сходится с биржей — 1)" in anomalies
