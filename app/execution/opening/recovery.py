"""Восстановление незавершённых открытий (§7 плана): при старте бота до
поллинга и каждые 15 с циклом openings.

Под тем же локом позиции, что «Открыть»: лок занят — открытие сейчас ведёт
другой обработчик, пропуск до следующего цикла. Новый вход не отправляется
ни в одной ветке:

- CONFIRMED — вход не уходил (строка ENTRY появляется вместе с SUBMITTING
  одним коммитом) → REFUSED «прервано перезапуском»;
- SUBMITTING / UNKNOWN — поиск по clientOrderId (flow.advance);
- FILLED / ALARM — защита позиции; PROTECTED — запись сделки.
WORKING (лимит на бирже) ведёт цикл лимитов, не этот модуль.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLock, position_lock_key
from app.core.logging import get_logger
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.database.session import Database
from app.exchanges.base import ExchangeError
from app.execution.opening.execution import Runner, transition
from app.execution.opening.flow import FlowOutcome, advance
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.trading.enums import OpeningStatus, TradeSide

logger = get_logger(__name__)

# (telegram_id, text, позиция для кнопки «Позиция» или None) — сообщение в чат.
Notify = Callable[[int, str, tuple[str, TradeSide] | None], Awaitable[None]]

RECOVERABLE = (
    OpeningStatus.CONFIRMED,
    OpeningStatus.SUBMITTING,
    OpeningStatus.UNKNOWN,
    OpeningStatus.FILLED,
    OpeningStatus.PROTECTED,
    OpeningStatus.ALARM,
)

_market_cache = TTLCache()


async def recover_openings(
    db: Database,
    settings: Settings,
    redis: Any,
    factory: Any,
    notify: Notify,
    *,
    statuses: tuple[OpeningStatus, ...] = RECOVERABLE,
    now: datetime | None = None,
) -> int:
    """Возвращает число открытий, которые сдвинулись с места."""
    moved = 0
    async with db.session() as session:
        ids = list(await session.scalars(
            select(TradeOpening.id).where(TradeOpening.status.in_(statuses))
            .order_by(TradeOpening.id)
        ))
    for opening_id in ids:
        try:
            if await _one(db, settings, redis, factory, notify, opening_id, now):
                moved += 1
        except Exception:
            # Одно открытие не роняет цикл остальных; повтор — следующим циклом.
            logger.exception("Восстановление открытия упало", extra={"opening_id": opening_id})
    return moved


async def _one(
    db: Database, settings: Settings, redis: Any, factory: Any, notify: Notify,
    opening_id: int, now: datetime | None,
) -> bool:
    async with db.session() as session:
        opening = await session.get(TradeOpening, opening_id)
        if opening is None or opening.status not in RECOVERABLE:
            return False
        user = await session.scalar(
            select(User).where(User.id == opening.user_id).options(selectinload(User.settings))
        )
        if user is None:
            return False
        key = position_lock_key(user.id, opening.symbol, opening.side.value)
        try:
            async with RedisLock(redis, key, settings.confirm_lock_ttl_seconds):
                await session.refresh(opening)
                before = opening.status
                outcome = await _advance(session, settings, factory, opening, now)
        except LockBusyError:
            return False
        if outcome is None:
            return False
        if outcome.notify and outcome.text:
            button = (opening.symbol, opening.side) if outcome.trade_id is not None else None
            await notify(user.telegram_id, outcome.text, button)
        if outcome.status is not before:
            logger.info(
                "Открытие восстановлено",
                extra={
                    "opening_id": opening.id, "from": before.value, "to": outcome.status.value,
                },
            )
            return True
        return False


async def _advance(
    session: Any, settings: Settings, factory: Any, opening: TradeOpening, now: datetime | None,
) -> FlowOutcome | None:
    if opening.status is OpeningStatus.CONFIRMED:
        await transition(
            session, opening, (OpeningStatus.CONFIRMED,), OpeningStatus.REFUSED,
            error_code="INTERRUPTED",
            error_message="Открытие прервано перезапуском бота до отправки входа.",
        )
        return FlowOutcome(
            OpeningStatus.REFUSED,
            f"ℹ️ Открытие {opening.symbol} {opening.side.value} прервано перезапуском бота до "
            "отправки входа. На бирже ничего нет — пересчитай карточку.",
        )
    try:
        client = await factory.for_user(session, opening.user_id, mode=opening.account_mode)
    except ExchangeError:
        logger.warning("Восстановление: клиента нет", extra={"opening_id": opening.id})
        return None
    try:
        info = await MarketDataService(client, _market_cache).get_symbol_info(opening.symbol)
        if info is None:
            return None
        runner = Runner(
            session, settings, client, opening,
            price_precision=info.price_precision, quantity_precision=info.quantity_precision,
        )
        return await advance(runner, now=now or datetime.now(UTC))
    except ExchangeError as exc:
        logger.warning(
            "Восстановление: биржа не ответила — повтор следующим циклом",
            extra={"opening_id": opening.id, "error": type(exc).__name__},
        )
        return None
    finally:
        await client.close()
