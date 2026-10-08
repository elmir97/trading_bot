"""Лимитный вход открытия (§6 плана): исполнение, частичное, истечение, отмена.

Ордер на бирже — GTC, срок держит бот: цикл openings (15 с) читает лимит по
clientOrderId и, когда срок вышел, отменяет его. «Отменить лимит» из чата или
Mini App — та же ветка отмены.

- PENDING, срок не вышел — ничего;
- PENDING, срок вышел → отмена → EXPIRED (исполнено 0) или сделка на
  исполненный объём;
- PARTIALLY_FILLED → сразу защита части: стоп с объёмом ≥ позиции или
  запасной closePosition-стоп (закрывает весь остаток и последующие части);
  сообщение один раз на каждое новое исполнение; сделка — когда лимит кончится;
- FILLED → защита и сделка (flow);
- отменён на бирже вручную → как отмена из бота.
Гонка «отмена ↔ исполнение»: DELETE ответил «ордера нет» — читаем исполнение.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.logging import get_logger
from app.core.numfmt import fmt_price, fmt_qty
from app.exchanges.base import ExchangeError, OrderFill, OrderNotFoundError
from app.execution.opening.execution import Runner, transition
from app.execution.opening.flow import FlowOutcome, advance, after_protect, alarm_text
from app.execution.opening.render import expiry_label
from app.trading.enums import OpeningStatus, OrderStatus

logger = get_logger(__name__)

_RESTING = ("PENDING", "NEW")
_CANCELLED = ("CANCELLED", "CANCELED", "EXPIRED")


async def _read(runner: Runner) -> OrderFill | None:
    try:
        return await runner.client.get_order_fill(
            runner.opening.symbol, runner.cid("e"), max_retries=1
        )
    except ExchangeError as exc:
        logger.warning(
            "Лимит открытия не прочитан — повтор следующим циклом",
            extra={**runner._log(), "code": exc.code},
        )
        return None


async def tick(runner: Runner, *, now: datetime | None = None) -> FlowOutcome | None:
    """Один проход по WORKING-лимиту. None — ничего не изменилось."""
    o = runner.opening
    moment = now or datetime.now(UTC)
    fill = await _read(runner)
    if fill is None:
        return None
    if fill.status == "FILLED" and fill.executed_qty > 0:
        await runner.apply_fill(fill)
        return await advance(runner, now=moment)
    if fill.status in _CANCELLED:
        return await _finish_cancelled(runner, fill, OpeningStatus.CANCELLED,
                                       "Лимит снят на бирже")
    if fill.executed_qty > 0:
        partial = await _protect_partial(runner, fill)
        if partial is not None:
            return partial
    if o.expires_at is not None and moment >= o.expires_at:
        return await cancel(runner, OpeningStatus.EXPIRED)
    return None


async def cancel(runner: Runner, final: OpeningStatus) -> FlowOutcome:
    """Отмена лимита (кнопка или срок). final — CANCELLED или EXPIRED."""
    o = runner.opening
    try:
        await runner.client.cancel_order_by_client_id(o.symbol, runner.cid("e"))
    except OrderNotFoundError:
        pass   # уже исполнен или снят — решает чтение ниже
    except ExchangeError as exc:
        logger.warning("Отмена лимита не прошла", extra={**runner._log(), "code": exc.code})
        return FlowOutcome(
            o.status, "Отменить лимит не удалось — биржа не ответила. Попробуй ещё раз.",
            notify=False,
        )
    fill = await _read(runner)
    if fill is None:
        return FlowOutcome(o.status, "Отмена отправлена, результат проверю.", notify=False)
    if fill.status == "FILLED" and fill.executed_qty > 0:
        await runner.apply_fill(fill)
        outcome = await advance(runner)
        return FlowOutcome(
            outcome.status, "Лимит исполнился раньше отмены.\n" + outcome.text, outcome.trade_id
        )
    if fill.status in _RESTING or fill.status == "PARTIALLY_FILLED":
        return FlowOutcome(o.status, "Отмена отправлена, лимит ещё на бирже — проверю.",
                           notify=False)
    why = "Срок лимита вышел" if final is OpeningStatus.EXPIRED else "Лимит отменён"
    return await _finish_cancelled(runner, fill, final, why)


async def _finish_cancelled(
    runner: Runner, fill: OrderFill, final: OpeningStatus, why: str
) -> FlowOutcome:
    o = runner.opening
    row = await runner.entry_row()
    if fill.executed_qty <= 0:
        if row is not None:
            row.status = OrderStatus.CANCELLED
            await runner.session.commit()
        await transition(runner.session, o, (OpeningStatus.WORKING,), final)
        when = (
            f" через {expiry_label(o.expiry_minutes)}"
            if final is OpeningStatus.EXPIRED and o.expiry_minutes else ""
        )
        icon = "⌛" if final is OpeningStatus.EXPIRED else "✖️"
        return FlowOutcome(
            final,
            f"{icon} {why}{when}: лимит {o.symbol} {o.side.value} @ "
            f"{fmt_price(o.limit_price, runner.pp)} снят. Позиция не открыта.",
        )
    # Частично исполнен: остаток снят — сделка на исполненный объём.
    await runner.apply_fill(fill)
    outcome = await advance(runner)
    return FlowOutcome(
        outcome.status,
        f"{why}: исполнено {fmt_qty(fill.executed_qty, runner.qp)} из "
        f"{fmt_qty(fill.orig_qty, runner.qp)}, остаток снят.\n" + outcome.text,
        outcome.trade_id,
    )


async def _protect_partial(runner: Runner, fill: OrderFill) -> FlowOutcome | None:
    """Часть исполнена, лимит стоит: стоп на всю позицию сейчас."""
    o = runner.opening
    position = await runner.position()
    if position is None:
        return None
    result = await runner.protect()
    previous = o.filled_qty or 0
    o.filled_qty = fill.executed_qty
    o.position_id = position.position_id
    await runner.session.commit()
    if result.status is OpeningStatus.PROTECTED:
        if fill.executed_qty == previous:
            return None
        return FlowOutcome(
            OpeningStatus.WORKING,
            f"◐ Лимит {o.symbol} {o.side.value} исполнен частично: "
            f"{fmt_qty(fill.executed_qty, runner.qp)} из {fmt_qty(fill.orig_qty, runner.qp)}, "
            f"стоп {fmt_price(o.stop_loss, runner.pp)} стоит на всю позицию. Остаток ждёт.",
        )
    # Стоп не встал: снимаем остаток лимита, позицию уже закрыли или ALARM.
    try:
        await runner.client.cancel_order_by_client_id(o.symbol, runner.cid("e"))
    except ExchangeError:
        logger.error("Остаток лимита не снят после сбоя стопа", extra=runner._log())
    await runner.apply_fill(fill)
    if result.status is OpeningStatus.EMERGENCY_CLOSED:
        return await after_protect(runner, result)
    await transition(
        runner.session, o, (OpeningStatus.FILLED,), OpeningStatus.ALARM,
        error_code="STOP_UNCONFIRMED" if result.undecided else "STOP_AND_CLOSE_FAILED",
        error_message=result.reason,
    )
    return FlowOutcome(
        OpeningStatus.ALARM, "◐ Лимит исполнен частично. " + alarm_text(runner, result)
    )
