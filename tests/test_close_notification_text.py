"""Текст уведомления reconciler о закрытии (хвост 29.09): у всех видов —
строка «Закрыта: DD.MM HH:MM» (app.core.timefmt) и суммы форматтерами
app.core.numfmt; комиссии без «+». Закрытие вне бота — ℹ️ и короткая
причина, полный exit_reason остаётся в журнале.

Без БД: Reconciler._close_text — статический метод над Trade и ExitFill.
Числа SOL #4 — живой стоп демо 27.09 (tests/fixtures, см.
test_reconciler_worker.py); LINK — вход #3 живой, выходы тейком, маркетом
и частичный — синтетика из живого (цены и комиссии подобраны по ставке
живого выхода LINK 15.055386 / (14.776 × 2037.8))."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.database.models.trade import Trade
from app.execution.reconciler import ExitFill
from app.trading.enums import ReconciliationKind, TradeSide, TradeStatus
from app.trading.exit_reasons import (
    EXIT_MANUAL_STOP,
    EXIT_MANUAL_TAKE,
    EXIT_OUTSIDE_BOT,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
)
from app.workers.reconciler import Reconciler

D = Decimal
EKB = 5  # Екатеринбург, пояс владельца


def _trade(
    *, trade_id: int, symbol: str, status: TradeStatus,
    pnl: str | None = None, fees: str | None = None,
) -> Trade:
    return Trade(
        id=trade_id, symbol=symbol, side=TradeSide.LONG, status=status,
        pnl=D(pnl) if pnl is not None else None,
        fees=D(fees) if fees is not None else None,
    )


def _exit(
    *, price: str, qty: str, fee: str, at: datetime, reason: str, kind: ReconciliationKind
) -> ExitFill:
    return ExitFill(
        order_id="1", price=D(price), quantity=D(qty), fee=D(fee),
        executed_at=at, reason=reason, kind=kind,
    )


LINK_CLOSED_AT = datetime(2026, 9, 29, 4, 3, 24, tzinfo=UTC)  # 09:03 Екб


def _link_closed(**kwargs: str) -> Trade:
    return _trade(trade_id=3, symbol="LINK-USDT", status=TradeStatus.CLOSED, **kwargs)


def test_bot_stop_loss_text() -> None:
    """Живой SOL #4: стоп бота 27.09 14:40:36 UTC. Раньше — fmt_decimal
    («82.821383», «-2087.121603») и без времени."""
    trade = _trade(
        trade_id=4, symbol="SOL-USDT", status=TradeStatus.CLOSED,
        pnl="-2087.121603", fees="166.602903",
    )
    fill = _exit(
        price="121.611", qty="1362.07", fee="82.821383",
        at=datetime(2026, 9, 27, 14, 40, 36, tzinfo=UTC),
        reason=EXIT_STOP_LOSS, kind=ReconciliationKind.CLOSED_STOP_LOSS,
    )

    assert Reconciler._close_text(trade, fill, "VST", EKB).splitlines() == [
        "🛑 SOL-USDT LONG закрыта по стопу на бирже",
        "Закрыта: 27.09 19:40",
        "Выход: 121.611 · объём 1362.07",
        "Комиссия выхода: 82.82 VST",
        "PnL: -2087.12 VST · комиссии вход+выход 166.60 VST",
        "📒 Сделка #4 закрыта в журнале",
    ]


def test_bot_take_profit_text() -> None:
    """(15.2 − 14.4) × 2037.8 − (14.672461 + 15.487280) = 1600.080259."""
    trade = _link_closed(pnl="1600.080259", fees="30.159741")
    fill = _exit(
        price="15.2", qty="2037.8", fee="15.487280", at=LINK_CLOSED_AT,
        reason=EXIT_TAKE_PROFIT, kind=ReconciliationKind.CLOSED_TAKE_PROFIT,
    )

    assert Reconciler._close_text(trade, fill, "VST", EKB).splitlines() == [
        "🎯 LINK-USDT LONG закрыта по тейку на бирже",
        "Закрыта: 29.09 09:03",
        "Выход: 15.2 · объём 2037.8",
        "Комиссия выхода: 15.49 VST",
        "PnL: +1600.08 VST · комиссии вход+выход 30.16 VST",
        "📒 Сделка #3 закрыта в журнале",
    ]


def test_outside_bot_manual_market_text() -> None:
    """Ручной маркет/лимит владельца: ℹ️, строки причины нет — «вне бота»
    уже в заголовке. (14.3 − 14.4) × 2037.8 − (14.672461 + 14.570270)."""
    trade = _link_closed(pnl="-233.022731", fees="29.242731")
    fill = _exit(
        price="14.3", qty="2037.8", fee="14.570270", at=LINK_CLOSED_AT,
        reason=EXIT_OUTSIDE_BOT, kind=ReconciliationKind.CLOSED_OUTSIDE_BOT,
    )

    assert Reconciler._close_text(trade, fill, "VST", EKB).splitlines() == [
        "ℹ️ LINK-USDT LONG закрыта на бирже вне бота",
        "Закрыта: 29.09 09:03",
        "Выход: 14.3 · объём 2037.8",
        "Комиссия выхода: 14.57 VST",
        "PnL: -233.02 VST · комиссии вход+выход 29.24 VST",
        "📒 Сделка #3 закрыта в журнале",
    ]


def test_outside_bot_manual_stop_text() -> None:
    """Живой LINK #3 29.09: ручной стоп в безубыток. В уведомлении —
    «Стоп, изменённый вручную», без хвоста «— закрыто вне бота»."""
    trade = _link_closed(pnl="736.484953", fees="29.727847")
    fill = _exit(
        price="14.776", qty="2037.8", fee="15.055386", at=LINK_CLOSED_AT,
        reason=EXIT_MANUAL_STOP, kind=ReconciliationKind.CLOSED_OUTSIDE_BOT,
    )

    assert Reconciler._close_text(trade, fill, "VST", EKB).splitlines() == [
        "ℹ️ LINK-USDT LONG закрыта на бирже вне бота",
        "Стоп, изменённый вручную",
        "Закрыта: 29.09 09:03",
        "Выход: 14.776 · объём 2037.8",
        "Комиссия выхода: 15.06 VST",
        "PnL: +736.48 VST · комиссии вход+выход 29.73 VST",
        "📒 Сделка #3 закрыта в журнале",
    ]


def test_outside_bot_manual_take_text() -> None:
    trade = _link_closed(pnl="1600.080259", fees="30.159741")
    fill = _exit(
        price="15.2", qty="2037.8", fee="15.487280", at=LINK_CLOSED_AT,
        reason=EXIT_MANUAL_TAKE, kind=ReconciliationKind.CLOSED_OUTSIDE_BOT,
    )

    assert Reconciler._close_text(trade, fill, "VST", EKB).splitlines() == [
        "ℹ️ LINK-USDT LONG закрыта на бирже вне бота",
        "Тейк, изменённый вручную",
        "Закрыта: 29.09 09:03",
        "Выход: 15.2 · объём 2037.8",
        "Комиссия выхода: 15.49 VST",
        "PnL: +1600.08 VST · комиссии вход+выход 30.16 VST",
        "📒 Сделка #3 закрыта в журнале",
    ]


def test_partial_close_text() -> None:
    """Частичное закрытие: сделка открыта, PnL ещё нет. ⚠️ остаётся —
    остаток позиции требует внимания."""
    trade = _trade(trade_id=3, symbol="LINK-USDT", status=TradeStatus.OPEN)
    fill = _exit(
        price="14.776", qty="1000", fee="7.388000", at=LINK_CLOSED_AT,
        reason=EXIT_STOP_LOSS, kind=ReconciliationKind.PARTIAL_CLOSE,
    )

    assert Reconciler._close_text(trade, fill, "VST", EKB).splitlines() == [
        "⚠️ LINK-USDT LONG частично закрыта на бирже",
        "Закрыта: 29.09 09:03",
        "Выход: 14.776 · объём 1000",
        "Комиссия выхода: 7.39 VST",
        "📒 Сделка #3 остаётся открытой",
    ]
