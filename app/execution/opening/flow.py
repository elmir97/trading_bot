"""Ход открытия от текущего статуса до устойчивого (§5, §7 плана).

advance() вызывают «Открыть» (сразу после отправки входа) и восстановление
(старт бота, цикл каждые 15 с): одно и то же продолжение для любого
незавершённого статуса. Вход здесь не отправляется никогда — только поиск
по clientOrderId, защита и журнал.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.logging import get_logger
from app.core.numfmt import fmt_price, fmt_qty
from app.database.models.trade_opening import TradeOpening
from app.execution.opening.execution import Mode, ProtectResult, Runner, transition
from app.execution.opening.render import expiry_label
from app.trading.enums import OpeningStatus, OrderStatus

logger = get_logger(__name__)

# Вход без ответа и не найден по cid: биржа могла не успеть его показать.
NOT_PLACED_AFTER = timedelta(seconds=30)
# Не найден, а позиция по стороне есть — после этого тревога.
UNKNOWN_ALARM_AFTER = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class FlowOutcome:
    status: OpeningStatus
    text: str
    trade_id: int | None = None
    notify: bool = True   # для восстановления: слать ли сообщение в чат


async def advance(runner: Runner, *, now: datetime | None = None) -> FlowOutcome:
    o = runner.opening
    moment = now or datetime.now(UTC)

    if o.status in (OpeningStatus.SUBMITTING, OpeningStatus.UNKNOWN):
        fill, not_found = await runner.read_fill()
        if fill is not None and fill.status == "FILLED" and fill.executed_qty > 0:
            await runner.apply_fill(fill)
        elif fill is not None and fill.status in ("PENDING", "NEW", "PARTIALLY_FILLED"):
            # Лимит стоит (или исполняется частично) — дальше цикл лимитов.
            await transition(
                runner.session, o, (OpeningStatus.SUBMITTING, OpeningStatus.UNKNOWN),
                OpeningStatus.WORKING, entry_order_id=fill.order_id,
                expires_at=_expires(o.expiry_minutes, moment),
            )
            return FlowOutcome(OpeningStatus.WORKING, working_text(runner), notify=True)
        elif not_found:
            age = moment - (o.decided_at or o.created_at)
            position = await runner.position()
            if position is None and age >= NOT_PLACED_AFTER:
                row = await runner.entry_row()
                if row is not None:
                    row.status = OrderStatus.NOT_PLACED
                    await runner.session.commit()
                await transition(
                    runner.session, o, (OpeningStatus.SUBMITTING, OpeningStatus.UNKNOWN),
                    OpeningStatus.NOT_PLACED, error_code="ENTRY_NOT_PLACED",
                    error_message="Вход не найден на бирже, позиции нет.",
                )
                return FlowOutcome(
                    OpeningStatus.NOT_PLACED,
                    f"ℹ️ Вход {o.symbol} {o.side.value} не выставлен на бирже (проверено по "
                    "clientOrderId). Позиция не открыта.",
                )
            if position is not None and age >= UNKNOWN_ALARM_AFTER:
                await transition(
                    runner.session, o, (OpeningStatus.SUBMITTING, OpeningStatus.UNKNOWN),
                    OpeningStatus.ALARM, error_code="ENTRY_NOT_FOUND_POSITION_EXISTS",
                    error_message="Вход не найден, а позиция по стороне есть.",
                )
                return FlowOutcome(
                    OpeningStatus.ALARM,
                    f"🚨 Вход {o.symbol} {o.side.value} не найден на бирже, а позиция по этой "
                    "стороне есть. Проверь её и стоп вручную.",
                )
            return FlowOutcome(o.status, unknown_text(runner), notify=False)
        else:
            return FlowOutcome(o.status, unknown_text(runner), notify=False)

    if o.status in (OpeningStatus.FILLED, OpeningStatus.ALARM) and o.filled_qty is not None:
        result = await runner.protect()
        return await after_protect(runner, result)

    if o.status is OpeningStatus.PROTECTED:
        trade_id = await runner.record_trade()
        await transition(
            runner.session, o, (OpeningStatus.PROTECTED,), OpeningStatus.DONE, trade_id=trade_id
        )
        return FlowOutcome(OpeningStatus.DONE, done_text(runner, ProtectResult(o.status)), trade_id)

    return FlowOutcome(o.status, "", notify=False)


async def after_protect(runner: Runner, result: ProtectResult) -> FlowOutcome:
    o = runner.opening
    if result.status is OpeningStatus.PROTECTED:
        await transition(
            runner.session, o, (OpeningStatus.FILLED, OpeningStatus.ALARM),
            OpeningStatus.PROTECTED,
        )
        trade_id = await runner.record_trade()
        await transition(
            runner.session, o, (OpeningStatus.PROTECTED,), OpeningStatus.DONE, trade_id=trade_id
        )
        return FlowOutcome(OpeningStatus.DONE, done_text(runner, result), trade_id)
    if result.status is OpeningStatus.EMERGENCY_CLOSED:
        trade_id = await runner.record_trade(result.close_fill, result.reason)
        await transition(
            runner.session, o, (OpeningStatus.FILLED, OpeningStatus.ALARM),
            OpeningStatus.EMERGENCY_CLOSED, trade_id=trade_id,
            error_code="STOP_FAILED", error_message=result.reason,
        )
        close = result.close_fill
        price = fmt_price(close.avg_price, runner.pp) if close else "—"
        return FlowOutcome(
            OpeningStatus.EMERGENCY_CLOSED,
            f"🚨 {result.reason}. Позиция {runner.summary()} закрыта маркетом по {price}.\n"
            f"Сделка #{trade_id} записана в журнал со входом и выходом.",
            trade_id,
        )
    first = o.status is not OpeningStatus.ALARM
    await transition(
        runner.session, o, (OpeningStatus.FILLED, OpeningStatus.ALARM), OpeningStatus.ALARM,
        error_code="STOP_UNCONFIRMED" if result.undecided else "STOP_AND_CLOSE_FAILED",
        error_message=result.reason,
    )
    if result.undecided:
        logger.warning("Стоп позиции открытия не подтверждён — перепроверка", extra=runner._log())
    else:
        logger.error("Позиция открытия без стопа, закрыть не удалось", extra=runner._log())
    return FlowOutcome(OpeningStatus.ALARM, alarm_text(runner, result), notify=first)


def alarm_text(runner: Runner, result: ProtectResult) -> str:
    o = runner.opening
    if result.undecided:
        return (
            f"⚠️ Стоп по позиции {o.symbol} {o.side.value} не подтверждён биржей — перепроверяю "
            "каждые 15 с. Позицию закрою, только если биржа подтвердит, что стопа нет. "
            "Проверь стоп в BingX."
        )
    return (
        f"🚨🚨 Позиция {o.symbol} {o.side.value} БЕЗ СТОПА: поставить стоп и закрыть маркетом "
        "не удалось. Закрой её вручную на бирже! Повторяю попытки каждые 15 с."
    )


def _expires(minutes: int | None, now: datetime) -> datetime | None:
    return now + timedelta(minutes=minutes) if minutes else None


_MODE_NOTE = {
    Mode.REPLACED.value: " (на всю позицию)",
    Mode.EXISTING.value: " (на всю позицию)",
    Mode.BACKUP.value: " (на всю позицию, поставлен отдельным ордером — вложенного не было)",
    Mode.KEPT.value: " (вложенный на объём входа)",
}


def done_text(runner: Runner, result: ProtectResult) -> str:
    o = runner.opening
    stop = f"стоп {fmt_price(o.stop_loss, runner.pp)} ✓{_MODE_NOTE.get(result.stop_mode, '')}"
    parts = [stop]
    if o.take_profit is not None:
        mark = "⚠️ не встал" if result.take_missing else (
            "✓" + _MODE_NOTE.get(result.take_mode, "")
        )
        parts.append(f"тейк {fmt_price(o.take_profit, runner.pp)} {mark}")
    lines = [
        f"✅ Открыто: {runner.summary()}",
        " · ".join(parts) + f" — сделка #{o.trade_id}",
    ]
    if result.take_missing:
        lines.append("Тейк не стоит — поставь его в «Позиции».")
    lines += [f"⚠️ {w}" for w in result.warnings]
    return "\n".join(lines)


def working_text(runner: Runner) -> str:
    o = runner.opening
    text = (
        f"⏳ Лимит {o.symbol} {o.side.value} {fmt_qty(o.quantity, runner.qp)} @ "
        f"{fmt_price(o.limit_price, runner.pp)}"
        f" выставлен, стоп {fmt_price(o.stop_loss, runner.pp)}"
    )
    if o.take_profit is not None:
        text += f", тейк {fmt_price(o.take_profit, runner.pp)}"
    if o.expiry_minutes:
        text += f". Срок {expiry_label(o.expiry_minutes)}"
    return text + "."


_WORKING_EXIT = {
    OpeningStatus.DONE: "✅ исполнен — итог ниже",
    OpeningStatus.EXPIRED: "⌛ срок вышел, лимит снят",
    OpeningStatus.CANCELLED: "✖️ снят на бирже",
    OpeningStatus.EMERGENCY_CLOSED: "🚨 исполнен, стоп не встал — итог ниже",
    OpeningStatus.ALARM: "🚨 исполнен, стоп не подтверждён — итог ниже",
}


def working_exit_text(opening: TradeOpening, status: OpeningStatus) -> str:
    """Короткий итог на месте ⏳ (Л3, решение владельца 09.10): лимит больше не
    стоит — кнопка «Отменить лимит» снимается, полный итог — новым сообщением."""
    state = _WORKING_EXIT.get(status, "завершён — итог ниже")
    return (
        f"⏳ Лимит {opening.symbol} {opening.side.value} @ "
        f"{fmt_price(opening.limit_price)}: {state}."
    )


def unknown_text(runner: Runner) -> str:
    o = runner.opening
    return (
        f"⏳ Биржа не подтвердила вход {o.symbol} {o.side.value} — проверяю по clientOrderId "
        "и сообщу итог. Повторно вход не отправляется."
    )
