"""Правка прежних итоговых сообщений открытия, когда оно сдвинулось (A.1,
10.10.2026) — для цикла восстановления и кнопки «🔴 Закрыть маркетом».

- retire_working(): лимит вышел из WORKING — ⏳ (карточка «Да» и сообщения
  OPEN_WORKING журнала исходящих) правится в короткий итог, кнопка «Отменить
  лимит» снимается (Л3, решение владельца 09.10);
- resolve_alarm(): тревога закончилась — сообщения OPEN_ALARM правятся в
  «✅ Решено: стоп поставлен в HH:MM» / «…закрыта аварийно…», кнопки
  («🔴 Закрыть маркетом») снимаются.

Полный итог уходит отдельным новым сообщением — здесь только старые. Ошибка
правки (удалено, старше 48 ч) — WARNING, дальше по списку; каждая правка —
в журнал исходящих (прежний текст — в edits).
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot import outbox
from app.core.logging import get_logger
from app.core.timefmt import fmt_local_datetime
from app.database.models.outgoing_message import OutgoingMessage, OutgoingMeta
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.database.session import Database
from app.execution.opening.flow import working_exit_text
from app.trading.enums import OpeningStatus
from app.trading.risk import tz_offset_for

logger = get_logger(__name__)


async def _targets(
    db: Database, opening_id: int, kind: str
) -> tuple[TradeOpening | None, User | None, set[tuple[int, int]]]:
    async with db.session() as session:
        opening = await session.get(TradeOpening, opening_id)
        if opening is None:
            return None, None, set()
        user = await session.scalar(
            select(User).where(User.id == opening.user_id).options(selectinload(User.settings))
        )
        rows = (await session.execute(
            select(OutgoingMessage.chat_id, OutgoingMessage.message_id).where(
                OutgoingMessage.trade_opening_id == opening_id, OutgoingMessage.kind == kind,
            )
        )).all()
    return opening, user, {(int(c), int(m)) for c, m in rows}


async def _rewrite(
    bot: Bot, db: Database, targets: set[tuple[int, int]], text: str, meta: OutgoingMeta,
    what: str,
) -> int:
    done = 0
    for chat_id, message_id in sorted(targets):
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except TelegramBadRequest as exc:
            logger.warning(
                "%s не исправлено", what, extra={
                    "opening_id": meta.trade_opening_id, "message_id": message_id,
                    "error": str(exc)[:120],
                },
            )
            continue
        await outbox.record(db, meta, chat_id=chat_id, message_id=message_id, text=text)
        done += 1
    return done


async def retire_working(
    bot: Bot, db: Database, opening_id: int, status: OpeningStatus | None
) -> int:
    opening, _, targets = await _targets(db, opening_id, f"OPEN_{OpeningStatus.WORKING.value}")
    if opening is None:
        return 0
    if opening.chat_id is not None and opening.card_message_id is not None:
        targets.add((opening.chat_id, opening.card_message_id))
    final = status or opening.status
    meta = OutgoingMeta(
        user_id=opening.user_id, kind=f"OPEN_WORKING_{final.value}",
        trade_opening_id=opening.id, trade_id=opening.trade_id,
    )
    return await _rewrite(bot, db, targets, working_exit_text(opening, final), meta,
                          "⏳ лимита")


def alarm_resolved_text(opening: TradeOpening, status: OpeningStatus, at: str) -> str:
    who = f"{opening.symbol} {opening.side.value}"
    if status is OpeningStatus.DONE:
        return f"🚨 → ✅ Решено: стоп поставлен в {at} — позиция {who} под защитой, итог ниже."
    if status is OpeningStatus.EMERGENCY_CLOSED:
        return f"🚨 → ✅ Решено: позиция {who} закрыта аварийно в {at} — итог ниже."
    return f"🚨 → Тревога по {who} снята в {at} — итог ниже."


async def resolve_alarm(
    bot: Bot, db: Database, opening_id: int, status: OpeningStatus,
    now: datetime | None = None,
) -> int:
    opening, user, targets = await _targets(db, opening_id, f"OPEN_{OpeningStatus.ALARM.value}")
    if opening is None or not targets:
        return 0
    offset = tz_offset_for(user.settings.timezone if user and user.settings else None)
    at = fmt_local_datetime(now or datetime.now(UTC), offset)[-5:]
    meta = OutgoingMeta(
        user_id=opening.user_id, kind=f"OPEN_ALARM_{status.value}",
        trade_opening_id=opening.id, trade_id=opening.trade_id,
    )
    return await _rewrite(bot, db, targets, alarm_resolved_text(opening, status, at), meta,
                          "Сообщение тревоги")
