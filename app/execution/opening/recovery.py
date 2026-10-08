"""Восстановление незавершённых открытий (§7 плана): при старте бота до
поллинга и каждые 15 с циклом openings.

Под тем же локом позиции, что «Открыть»: лок занят — открытие сейчас ведёт
другой обработчик, пропуск до следующего цикла. Новый вход не отправляется
ни в одной ветке:

- CONFIRMED — вход не уходил (строка ENTRY появляется вместе с SUBMITTING
  одним коммитом) → REFUSED «прервано перезапуском»;
- SUBMITTING / UNKNOWN — поиск по clientOrderId (flow.advance);
- FILLED / ALARM — защита позиции; PROTECTED — запись сделки;
- WORKING — лимит на бирже: исполнение, частичное, истечение (limits.tick).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLock, position_lock_key
from app.core.logging import get_logger
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.database.session import Database
from app.exchanges.base import ExchangeError
from app.execution.opening import limits
from app.execution.opening.execution import Runner, transition
from app.execution.opening.flow import FlowOutcome, advance
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.trading.enums import OPENING_ACTIVE, OpeningStatus, TradeSide

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
    OpeningStatus.WORKING,
)

_market_cache = TTLCache()


async def opening_in_flight(
    session: Any, user_id: int, symbol: str, side: TradeSide
) -> bool:
    """Незавершённое открытие из бота по этой позиции (вход в полёте, лимит
    стоит или исполнен частично, сделка не записана)."""
    found = await session.scalar(
        select(TradeOpening.id).where(
            TradeOpening.user_id == user_id, TradeOpening.symbol == symbol,
            TradeOpening.side == side, TradeOpening.status.in_(OPENING_ACTIVE),
        ).limit(1)
    )
    return found is not None


async def expire_stale_cards(db: Database, settings: Settings) -> int:
    """Карточки CARD старше срока → EXPIRED_CARD (только статус в базе: кнопки
    не снимаются — нажатие «Открыть» на текущей карточке покажет пересчёт)."""
    moment = datetime.now(UTC) - timedelta(seconds=settings.exec_open_card_ttl_seconds)
    async with db.session() as session:
        result = await session.execute(
            update(TradeOpening)
            .where(TradeOpening.status == OpeningStatus.CARD, TradeOpening.created_at < moment)
            .values(
                status=OpeningStatus.EXPIRED_CARD, error_code="CARD_EXPIRED",
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


UNCONFIRMED_KEY = "opening:unconfirmed:{id}"


async def remind_unconfirmed(
    redis: Any, settings: Settings, opening: TradeOpening, telegram_id: int, notify: Notify,
    now: datetime,
) -> None:
    """«Стоп не подтверждён» (ALARM без закрытия, решение владельца 08.10): через
    exec_open_unconfirmed_alarm_seconds — повторная тревога, дальше напоминание
    раз в exec_open_unconfirmed_remind_seconds, пока не решится. Без закрытия.
    Время — в Redis: переживает рестарт бота."""
    key = UNCONFIRMED_KEY.format(id=opening.id)
    if opening.status is not OpeningStatus.ALARM or opening.error_code != "STOP_UNCONFIRMED":
        await redis.delete(key)
        return
    ts = now.timestamp()
    first = await redis.hget(key, "first")
    if first is None:
        await redis.hset(key, mapping={"first": ts})
        await redis.expire(key, 86400)
        return
    first_ts = float(first)
    last = await redis.hget(key, "last")
    who = f"{opening.symbol} {opening.side.value}"
    if last is None:
        if ts - first_ts < settings.exec_open_unconfirmed_alarm_seconds:
            return
        minutes = int(settings.exec_open_unconfirmed_alarm_seconds // 60)
        text = f"🚨 Стоп не подтверждён {minutes} мин — проверь позицию {who} на бирже вручную."
    else:
        if ts - float(last) < settings.exec_open_unconfirmed_remind_seconds:
            return
        minutes = int((ts - first_ts) // 60)
        text = (
            f"🚨 Стоп всё ещё не подтверждён ({minutes} мин) — проверь позицию {who} на "
            "бирже вручную. Позицию бот не закрывает, пока биржа не подтвердит, что стопа нет."
        )
    await notify(telegram_id, text, None)
    await redis.hset(key, mapping={"last": ts})
    logger.warning(
        "Напоминание: стоп не подтверждён", extra={"opening_id": opening.id, "minutes": minutes}
    )


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
        await session.refresh(opening)
        await remind_unconfirmed(
            redis, settings, opening, user.telegram_id, notify, now or datetime.now(UTC)
        )
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
        moment = now or datetime.now(UTC)
        if opening.status is OpeningStatus.WORKING:
            return await limits.tick(runner, now=moment)
        return await advance(runner, now=moment)
    except ExchangeError as exc:
        logger.warning(
            "Восстановление: биржа не ответила — повтор следующим циклом",
            extra={"opening_id": opening.id, "error": type(exc).__name__},
        )
        return None
    finally:
        await client.close()
