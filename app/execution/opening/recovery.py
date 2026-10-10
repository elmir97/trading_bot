"""Восстановление незавершённых открытий (§7 плана): при старте бота до
поллинга и каждые 15 с циклом openings.

Под тем же локом позиции, что «Открыть»: лок занят — открытие сейчас ведёт
другой обработчик, пропуск до следующего цикла. Новый вход не отправляется
ни в одной ветке:

- CONFIRMED — вход не уходил (строка ENTRY появляется вместе с SUBMITTING
  одним коммитом) → REFUSED «прервано перезапуском»;
- SUBMITTING / UNKNOWN — поиск по clientOrderId (flow.advance);
- FILLED / ALARM — защита позиции; PROTECTED — запись сделки (вход по ответу
  POST — после дочитки цены входа); EMERGENCY_CLOSED с ENTRY_FILL_UNREAD —
  дописать сделку аварийного закрытия;
- WORKING — лимит на бирже: исполнение, частичное, истечение (limits.tick).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLock, close_wanted_key, position_lock_key
from app.core.logging import get_logger
from app.database.models.outgoing_message import OutgoingMeta
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.database.session import Database
from app.exchanges.base import ExchangeError
from app.execution.opening import limits
from app.execution.opening.execution import Runner, transition
from app.execution.opening.flow import ENTRY_FILL_UNREAD, FlowOutcome, advance
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.trading.enums import OPENING_ACTIVE, OpeningStatus, TradeSide

logger = get_logger(__name__)

class Notify(Protocol):
    """Сообщение в чат: (telegram_id, text, позиция для кнопки «Позиция» или
    None); meta — к чему оно относится (журнал исходящих, A.2); status —
    статус открытия (клавиатура итога, A.1); retire_working — id открытия,
    вышедшего из WORKING: его ⏳ правится в короткий итог без кнопки (Л3);
    resolve_alarm — id открытия, вышедшего из ALARM: сообщение тревоги
    правится в «✅ Решено…» без кнопок (A.1)."""

    def __call__(
        self, telegram_id: int, text: str, position: tuple[str, TradeSide] | None, *,
        meta: OutgoingMeta | None = None, status: OpeningStatus | None = None,
        retire_working: int | None = None, resolve_alarm: int | None = None,
    ) -> Awaitable[None]: ...

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


def _emergency_unread() -> Any:
    """Аварийно закрыто, сделка не записана — вход не дочитался (деплой 3).
    По коду, не по trade_id IS NULL: у старых открытий trade_id обнулило
    удаление сделки (SET NULL), их дописывать нельзя."""
    return (TradeOpening.status == OpeningStatus.EMERGENCY_CLOSED) & (
        TradeOpening.error_code == ENTRY_FILL_UNREAD
    )


def _is_emergency_unread(opening: TradeOpening) -> bool:
    return (
        opening.status is OpeningStatus.EMERGENCY_CLOSED
        and opening.error_code == ENTRY_FILL_UNREAD
    )


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
    await notify(telegram_id, text, None, meta=OutgoingMeta(
        user_id=opening.user_id, kind="OPEN_ALARM_REMINDER", trade_opening_id=opening.id,
    ))
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
    on_alarm: Callable[[int], None] | None = None,
) -> int:
    """Возвращает число открытий, которые сдвинулись с места. on_alarm(id) —
    открытие только что перешло в ALARM (быстрые повторы, B.1)."""
    moved = 0
    async with db.session() as session:
        ids = list(await session.scalars(
            select(TradeOpening.id).where(
                TradeOpening.status.in_(statuses) | _emergency_unread()
            ).order_by(TradeOpening.id)
        ))
    for opening_id in ids:
        try:
            if await _one(db, settings, redis, factory, notify, opening_id, now, on_alarm):
                moved += 1
        except Exception:
            # Одно открытие не роняет цикл остальных; повтор — следующим циклом.
            logger.exception("Восстановление открытия упало", extra={"opening_id": opening_id})
    return moved


async def recover_one(
    db: Database, settings: Settings, redis: Any, factory: Any, notify: Notify,
    opening_id: int,
) -> bool:
    """Один проход по одному открытию (быстрый повтор после тревоги, B.1) —
    тот же лок и те же ветки, что у цикла."""
    return await _one(db, settings, redis, factory, notify, opening_id, None, None)


async def _one(
    db: Database, settings: Settings, redis: Any, factory: Any, notify: Notify,
    opening_id: int, now: datetime | None, on_alarm: Callable[[int], None] | None,
) -> bool:
    async with db.session() as session:
        opening = await session.get(TradeOpening, opening_id)
        if opening is None or not (opening.status in RECOVERABLE or _is_emergency_unread(opening)):
            return False
        user = await session.scalar(
            select(User).where(User.id == opening.user_id).options(selectinload(User.settings))
        )
        if user is None:
            return False
        if opening.status is OpeningStatus.ALARM and await redis.exists(
            close_wanted_key(opening.id)
        ):
            # Владелец нажал «🔴 Да, закрыть» и ждёт лок — повтор защиты уступает
            # (деплой 3, фикс 5; на 🔴 10.10 повтор B.1 отнял у кнопки лок).
            logger.info("Повтор после тревоги уступил кнопке «Закрыть»",
                        extra={"opening_id": opening.id})
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
        if (
            on_alarm is not None and outcome.status is OpeningStatus.ALARM
            and before is not OpeningStatus.ALARM
        ):
            on_alarm(opening.id)
        if outcome.notify and outcome.text:
            # «Позиция» — у записанной сделки, под тревогой (A.1: ALARM —
            # «⚙️ Позиция» и «🔴 Закрыть маркетом») и под стопом без сделки.
            button = (
                (opening.symbol, opening.side)
                if outcome.trade_id is not None
                or outcome.status in (OpeningStatus.ALARM, OpeningStatus.PROTECTED)
                else None
            )
            left_working = (
                before is OpeningStatus.WORKING and outcome.status is not OpeningStatus.WORKING
            )
            left_alarm = (
                before is OpeningStatus.ALARM and outcome.status is not OpeningStatus.ALARM
            )
            await notify(
                user.telegram_id, outcome.text, button, meta=OutgoingMeta(
                    user_id=user.id, kind=f"OPEN_{outcome.status.value}",
                    trade_opening_id=opening.id, trade_id=outcome.trade_id,
                ), status=outcome.status,
                retire_working=opening.id if left_working else None,
                resolve_alarm=opening.id if left_alarm else None,
            )
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
