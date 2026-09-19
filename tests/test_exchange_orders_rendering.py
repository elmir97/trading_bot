"""_render_open_order (app/bot/handlers/exchange.py) — рендер выставленных
ордеров с биржи в разделе "Ордера".

Раньше эта ветка не выполнялась ни в одном тесте, ни в smoke_check (там
только путь "без ключей"), ни в simulate_chat — её достигают только
подключённый рабочий ключ И реально выставленный ордер на бирже. Из-за
этого NameError (fmt_price не был импортирован, F821) молчал: пользователь
видел не саму ошибку, а обманчивое "Не удалось получить данные с биржи"
из общего except в _with_exchange.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

from app.bot.handlers.exchange import _render_open_order
from app.exchanges.base import AttachedTpSl, OpenOrder, SymbolInfo

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _order(**overrides) -> OpenOrder:
    fields = dict(
        order_id="1",
        client_order_id="tj1_1_entry",
        symbol="BTC-USDT",
        side="BUY",
        position_side="LONG",
        order_type="LIMIT",
        quantity=D("0.01"),
        executed_qty=D("0"),
        price=D("100123.456"),
        stop_price=D("0"),
        status="NEW",
        leverage=10,
        reduce_only=False,
        close_position=False,
        working_type="MARK_PRICE",
        created_at=_NOW,
        updated_at=_NOW,
        take_profit=None,
        stop_loss=None,
    )
    fields.update(overrides)
    return OpenOrder(**fields)


def test_renders_price_take_profit_and_stop_loss_with_symbol_precision() -> None:
    precision = SymbolInfo(
        symbol="BTC-USDT", price_precision=1, quantity_precision=3,
        min_quantity=D("0.001"), max_leverage=125,
    )
    order = _order(
        take_profit=AttachedTpSl(
            trigger_price=D("103000.789"), price=D("103000.789"),
            quantity=D("0.01"), working_type="MARK_PRICE",
        ),
        stop_loss=AttachedTpSl(
            trigger_price=D("99000.123"), price=D("99000.123"),
            quantity=D("0.01"), working_type="MARK_PRICE",
        ),
    )

    text = _render_open_order(order, precision)

    assert "по 100123.5" in text  # price_precision=1, ROUND_HALF_UP
    assert "Тейк: 103000.8" in text
    assert "Стоп: 99000.1" in text


def test_renders_without_symbol_precision_and_without_tp_sl() -> None:
    order = _order()

    text = _render_open_order(order, precision=None)

    assert "по 100123.46" in text  # без SymbolInfo — fallback-точность fmt_price
    assert "Тейк" not in text
    assert "Стоп" not in text
