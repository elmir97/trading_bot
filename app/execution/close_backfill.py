"""Добор строк закрытия по cid (деплой 4, 4.4).

11.10, #37: «Закрыть всё» закрыло позицию, а чтение по cid в первые ~2 с не
дало FILLED — строка CLOSE осталась UNKNOWN навсегда, действие FAILED «не
подтверждено». С 4.2/4.3 закрытие по FILLED из ответа POST — строка FILLED с
price NULL («не дочитано»). Обе дочитывает reconciler: GET по cid →
статус, orderId, цена, объём, комиссия.

Правило владельца (12.10): действие FAILED «не подтверждено», чья строка
закрытия добрана в FILLED, — DONE, ошибка очищена, в params пометка
«подтверждено добором».

На 109421 строку не трогаем (только INFO): у строки нет счёта, а ответ
клиента другого режима (DEMO/LIVE) — тоже «order not exist»."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.exchanges.base import ExchangeClient, ExchangeError
from app.execution.readback_log import readback_failure
from app.trading.enums import OrderRole, OrderStatus, PositionActionStatus

logger = get_logger(__name__)

# Окно истории BingX — дальше строку уже не проверить.
BACKFILL_WINDOW = timedelta(days=7)
# Не больше строк за проход: GET по cid на каждую.
BACKFILL_MAX_ROWS = 5
# Сообщение отказа действия, которое снимает добор (position_action_service).
UNCONFIRMED_MESSAGE = "Исполнение закрытия не подтверждено."
CONFIRMED_MARK = "добор по cid"


def _due() -> object:
    return or_(
        ExecutionOrder.status == OrderStatus.UNKNOWN,
        and_(ExecutionOrder.status == OrderStatus.FILLED, ExecutionOrder.price.is_(None)),
    )


async def backfill_close_rows(
    session: AsyncSession, client: ExchangeClient, user_id: int, now: datetime
) -> list[int]:
    """Один проход. Возвращает id добранных строк. GET — только если такие
    строки есть. Коммит — за вызывающим."""
    rows = list(await session.scalars(
        select(ExecutionOrder).where(
            ExecutionOrder.user_id == user_id,
            ExecutionOrder.role == OrderRole.CLOSE,
            ExecutionOrder.client_order_id.is_not(None),
            ExecutionOrder.created_at >= now - BACKFILL_WINDOW,
            _due(),  # type: ignore[arg-type]
        ).order_by(ExecutionOrder.id).limit(BACKFILL_MAX_ROWS)
    ))
    done: list[int] = []
    for row in rows:
        log = {"user_id": user_id, "execution_order_id": row.id, "cid": row.client_order_id,
               "prev_status": row.status.value}
        try:
            fill = await client.get_order_fill(
                row.symbol, row.client_order_id or "", max_retries=1
            )
        except ExchangeError as exc:
            logger.info("Строка закрытия не добрана по cid",
                        extra={**log, **readback_failure(error=exc)})
            continue
        if fill.status != "FILLED" or fill.executed_qty <= 0:
            logger.info("Строка закрытия не добрана по cid",
                        extra={**log, **readback_failure(fill=fill)})
            continue
        row.status = OrderStatus.FILLED
        row.exchange_order_id = fill.order_id or row.exchange_order_id
        row.price = fill.avg_price
        row.quantity = fill.executed_qty
        row.raw_response = {
            "status": fill.status, "avgPrice": str(fill.avg_price),
            "executedQty": str(fill.executed_qty), "commission": str(fill.fee),
            "source": CONFIRMED_MARK,
        }
        done.append(row.id)
        logger.info("Строка закрытия добрана по cid",
                    extra={**log, "price": str(fill.avg_price), "qty": str(fill.executed_qty)})
        if row.position_action_id is not None:
            await _confirm_action(session, row.position_action_id, now)
    return done


async def _confirm_action(session: AsyncSession, action_id: int, now: datetime) -> None:
    action = await session.get(PositionAction, action_id)
    if (
        action is None or action.status is not PositionActionStatus.FAILED
        or action.error_message != UNCONFIRMED_MESSAGE
    ):
        return
    action.status = PositionActionStatus.DONE
    action.error_code = None
    action.error_message = None
    action.params = {**(action.params or {}), "confirmed_by": CONFIRMED_MARK,
                     "confirmed_at": now.isoformat()}
    logger.info("Действие подтверждено добором", extra={"action_id": action.id})
