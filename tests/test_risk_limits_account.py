"""Объём от риска, лимиты убытка по счёту, формат процентов (03.10.2026).

Живые случаи проверки владельца 03.10 (мастер «Добавить сделку»):
- вход 84590, стоп 84000, баланс 1000, риск 2% → объём округлялся вверх,
  «Риск 2.00%, допустимый — 2.0000%» засчитывался нарушением;
- «Недельный лимит убытка достигнут: −224.54%» — сумма PnL всех сделок
  (демо по сигналам, счёт ~85 000 VST) делилась на введённые 1000.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.core.numfmt import fmt_pct
from app.database.models.trade import Trade
from app.database.models.trading_plan import TradingPlan
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import SymbolInfo
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.position_actions import _check_cap
from app.services.user_service import UserService
from app.trading.calculations import CalculationError, calculate_position_size
from app.trading.enums import ExchangeKeyMode, TradeSide, TradeSource, TradeStatus
from app.trading.risk import PlanValidator, ViolationCode
from tests.conftest import cleanup_user
from tests.test_position_actions import _inputs

D = Decimal
# Суббота 03.10.2026 11:46 по Екатеринбургу — момент проверки владельца.
NOW = datetime(2026, 10, 3, 6, 46, tzinfo=UTC)


# --- объём от риска -------------------------------------------------------------


def _live_sizing(step: Decimal | None = None):  # type: ignore[no-untyped-def]
    return calculate_position_size(
        account_balance=D(1000), risk_percent=D(2), entry_price=D(84590),
        stop_loss=D(84000), side=TradeSide.LONG, quantity_step=step,
    )


def test_quantity_rounds_down_without_lot_step() -> None:
    sizing = _live_sizing()
    # 20 / 590 = 0.0338983050847… → вниз до 8 знаков, не 0.03389831.
    assert sizing.quantity == D("0.03389830")
    assert sizing.risk_actual <= sizing.risk_amount == D(20)


def test_quantity_rounds_down_to_lot_step() -> None:
    sizing = _live_sizing(D("0.0001"))
    assert sizing.quantity == D("0.0338")
    assert sizing.risk_actual == D("19.942")
    assert sizing.quantity_step == D("0.0001")


def test_quantity_below_lot_step_is_error() -> None:
    with pytest.raises(CalculationError, match="шага лота"):
        _live_sizing(D(1))


def test_fmt_pct_two_digits() -> None:
    assert fmt_pct(D("2.0000")) == "2.00%"
    assert fmt_pct(D("10.0000")) == "10.00%"
    assert fmt_pct(D("2.005")) == "2.01%"


# --- потолок этапа 4 ------------------------------------------------------------


def test_stage4_cap_compares_cents() -> None:
    inputs = _inputs(equity=D(1000), risk_cap_percent=D("2.0000"))
    assert _check_cap(inputs, D("20.004")) is None          # «20.00 больше 20.00» — нет
    refusal = _check_cap(inputs, D("20.006"))
    assert refusal is not None and refusal.code is Code.RISK_CAP_EXCEEDED
    assert "2.00% = 20.00 USDT" in refusal.message
    assert "2.0000" not in refusal.message


def test_stage4_min_distance_percent_format() -> None:
    from app.execution.position_actions import plan_action
    from app.trading.enums import PositionActionKind as Kind

    refusal = plan_action(
        Kind.MOVE_STOP, {"level": "1.5599"},
        _inputs(min_distance_percent=D("0.1000"),
                symbol_info=SymbolInfo("XRP-USDT", 4, 0, D(2), D(2))),
    )
    assert "0.10%" in refusal.message and "0.1000" not in refusal.message  # type: ignore[union-attr]


# --- проверка плана и лимиты по счёту (БД) --------------------------------------

db_only = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


def _plan() -> TradingPlan:
    # Как в базе прода: Numeric(…, 4) — Decimal("2.0000").
    return TradingPlan(
        user_id=0, risk_per_trade_percent=D("2.0000"), max_daily_loss_percent=D("6.0000"),
        max_weekly_loss_percent=D("10.0000"), max_trades_per_day=5,
        min_risk_reward=D("2.0000"), max_leverage=20, allowed_symbols=[],
        allowed_timeframes=[],
    )


@pytest_asyncio.fixture
async def env(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        )
        user = await svc.get_or_create(telegram_id=unique_telegram_id())
        yield session, user, TradeRepository(session)
        await cleanup_user(session, user)
    await db.dispose()


async def _closed(  # type: ignore[no-untyped-def]
    session, user, pnl: str, balance: str | None, mode: ExchangeKeyMode | None,
    closed_at: datetime = NOW - timedelta(days=2),
) -> None:
    session.add(Trade(
        user_id=user.id, symbol="ETH-USDT", side=TradeSide.LONG, entry_price=D(2700),
        exit_price=D(2660), quantity=D(1), status=TradeStatus.CLOSED,
        source=TradeSource.MANUAL if mode is None else TradeSource.SIGNAL_EXECUTION,
        account_mode=mode, pnl=D(pnl),
        account_balance_at_entry=D(balance) if balance else None,
        opened_at=closed_at - timedelta(hours=2), closed_at=closed_at,
        fills=[], mistakes=[],
    ))
    await session.flush()


async def _check(repo, user, mode, *, quantity=D("0.033898305085"), balance=D(1000)):  # type: ignore[no-untyped-def]
    return await PlanValidator(repo).check(
        plan=_plan(), user_id=user.id, symbol="BTC-USDT", side=TradeSide.LONG,
        entry_price=D(84590), quantity=quantity, stop_loss=D(84000),
        take_profit=D(87000), leverage=20, timeframe=None, account_balance=balance,
        account_mode=mode, now=NOW,
    )


@db_only
async def test_live_case_risk_2_percent_is_not_violation(env) -> None:  # type: ignore[no-untyped-def]
    _session, user, repo = env
    # Объём, записанный 03.10 (0.033898305085), и показанный (0.03389831) —
    # риск на хвост выше 2%, при показе 2.00% — не нарушение.
    for qty in (D("0.033898305085"), D("0.03389831")):
        result = await _check(repo, user, None, quantity=qty)
        assert ViolationCode.RISK_TOO_HIGH not in {v.code for v in result.violations}


@db_only
async def test_risk_violation_message_unified_format(env) -> None:  # type: ignore[no-untyped-def]
    _session, user, repo = env
    result = await _check(repo, user, None, quantity=D("0.04"))
    [message] = [v.message for v in result.violations if v.code is ViolationCode.RISK_TOO_HIGH]
    assert message == "Риск 2.36%, допустимый — 2.00%."


@db_only
async def test_demo_losses_do_not_count_for_manual_journal(env) -> None:  # type: ignore[no-untyped-def]
    """Живой случай: −2245 VST демо-сделок при введённых 1000 давали −224.54%."""
    session, user, repo = env
    await _closed(session, user, "-2339.91", "87177.86", ExchangeKeyMode.DEMO)
    await _closed(session, user, "-1891.28", "84837.42", ExchangeKeyMode.DEMO)
    result = await _check(repo, user, None)
    assert ViolationCode.WEEKLY_LOSS_LIMIT not in {v.code for v in result.violations}
    assert not result.notes


@db_only
async def test_weekly_percent_from_each_trade_balance(env) -> None:  # type: ignore[no-untyped-def]
    session, user, repo = env
    # −6% и −5% от своих балансов = −11% ≥ лимита 10%; сделки другого счёта
    # и ручные в сумму не входят.
    await _closed(session, user, "-600", "10000", ExchangeKeyMode.LIVE)
    await _closed(session, user, "-50", "1000", ExchangeKeyMode.LIVE)
    await _closed(session, user, "-5000", "1000", ExchangeKeyMode.DEMO)
    await _closed(session, user, "-5000", "1000", None)
    result = await _check(repo, user, ExchangeKeyMode.LIVE)
    [message] = [v.message for v in result.violations if v.code is ViolationCode.WEEKLY_LOSS_LIMIT]
    assert message == "Недельный лимит убытка достигнут: −11.00% при лимите 10.00%."


@db_only
async def test_trades_without_balance_counted_separately(env) -> None:  # type: ignore[no-untyped-def]
    session, user, repo = env
    await _closed(session, user, "-1.30", None, ExchangeKeyMode.DEMO)
    await _closed(session, user, "-1.26", None, ExchangeKeyMode.DEMO)
    await _closed(session, user, "-10", "1000", ExchangeKeyMode.DEMO)
    period = await repo.pnl_percent_between(
        user.id, NOW - timedelta(days=7), NOW, account_mode=ExchangeKeyMode.DEMO
    )
    assert (period.percent, period.counted, period.uncounted) == (D(-1), 1, 2)
    result = await _check(repo, user, ExchangeKeyMode.DEMO)
    assert result.ok
    assert "Убыток недели, не учтены: 2 (сделки без баланса на входе)." in result.notes
    assert "не учтены: 2" in result.render()


@db_only
async def test_daily_trade_count_same_account_only(env) -> None:  # type: ignore[no-untyped-def]
    session, user, repo = env
    for _ in range(5):
        await _closed(session, user, "1", "1000", ExchangeKeyMode.DEMO, closed_at=NOW)
    manual = await _check(repo, user, None)
    assert ViolationCode.DAILY_TRADE_LIMIT not in {v.code for v in manual.violations}
    demo = await _check(repo, user, ExchangeKeyMode.DEMO)
    assert ViolationCode.DAILY_TRADE_LIMIT in {v.code for v in demo.violations}


# --- мастер: шаг «Объём от риска» ----------------------------------------------


@pytest.mark.parametrize(
    ("step", "qty", "line"),
    [
        (D("0.0001"), "0.0338", "Объём: 0.0338 (вниз до шага лота BingX 0.0001)"),
        (None, "0.0338983", "Объём: 0.0338983 (вниз до 8 знаков, шаг лота биржи неизвестен)"),
    ],
)
async def test_wizard_auto_quantity_rounds_down(monkeypatch, step, qty, line) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Message

    from app.bot.handlers import trades as handlers
    from app.bot.states.trade import AddTradeStates

    class _Repo:
        def __init__(self, session):  # type: ignore[no-untyped-def]
            pass

        async def get_trading_plan(self, user_id):  # type: ignore[no-untyped-def]
            return _plan()

    async def fake_step(settings, symbol):  # type: ignore[no-untyped-def]
        assert symbol == "BTC-USDT"
        return step

    monkeypatch.setattr(handlers, "UserRepository", _Repo)
    monkeypatch.setattr(handlers, "lot_step", fake_step)
    monkeypatch.setattr(handlers, "_show_leverage_prompt", AsyncMock())
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=2, user_id=2))
    await state.set_state(AddTradeStates.quantity)
    await state.update_data(
        symbol="BTC-USDT", side="LONG", entry_price="84590", stop_loss="84000",
        quantity_mode="auto",
    )
    message = MagicMock(spec=Message)
    message.text = "1000"
    message.answer = AsyncMock()
    await handlers.set_quantity(message, state, SimpleNamespace(id=1), None, None)  # type: ignore[arg-type]

    text = message.answer.await_args.args[0]
    assert line in text
    risk = "19.94 USDT (1.99%)" if step else "20.00 USDT (2.00%)"
    assert f"Риск по объёму: {risk}" in text
    assert D((await state.get_data())["quantity"]) == D(qty)
