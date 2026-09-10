"""Раздел «🧠 Анализ ошибок» — разбор журнала.

Handler не считает и не ходит в API синхронно: он ставит заглушку,
запускает фоновую задачу и редактирует сообщение по готовности.
Event loop не блокируется, Telegram не ловит таймаут.

DatabaseMiddleware закрывает свою сессию сразу по возврату из handler'а
(см. app/bot/middlewares/database.py), а фоновая задача переживает
handler. Поэтому `_analyze_and_reply` открывает собственную сессию через
`db.session()` вместо переиспользования сессии запроса — иначе разбор
падает на уже закрытой сессии.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis import reports
from app.analysis.ai.service import AIAnalysisService
from app.analysis.insights.loader import load_facts
from app.bot.keyboards.insights import InsightsCallback, insights_menu, periods_keyboard
from app.database.repositories.ai_report import AIReportRepository
from app.database.session import Database

logger = logging.getLogger(__name__)

router = Router(name="insights")

PERIOD_DAYS = {"week": 7, "month": 30, "quarter": 90}
COOLDOWN = timedelta(minutes=10)


@router.callback_query(F.data == "insights:menu")
async def open_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🧠 <b>Разбор журнала</b>\n\nВыбери период для анализа.",
        reply_markup=periods_keyboard(),
    )
    await callback.answer()


@router.callback_query(InsightsCallback.filter(F.action == "analyze"))
async def run_analysis(
    callback: CallbackQuery,
    callback_data: InsightsCallback,
    session,
    user,
    db: Database,
    build_ai_service: Callable[[AsyncSession], AIAnalysisService],
) -> None:
    days = PERIOD_DAYS.get(callback_data.period, 30)
    repo = AIReportRepository(session)

    last = await repo.last_created_at(user.id)
    if last is not None and datetime.now(tz=timezone.utc) - last < COOLDOWN and not callback_data.force:
        left = COOLDOWN - (datetime.now(tz=timezone.utc) - last)
        await callback.answer(
            f"Следующий разбор через {int(left.total_seconds() // 60) + 1} мин",
            show_alert=True,
        )
        return

    await callback.message.edit_text("⏳ Собираю статистику и разбираю сделки…")
    await callback.answer()

    asyncio.create_task(
        _analyze_and_reply(
            callback=callback,
            db=db,
            user_id=user.id,
            days=days,
            build_ai_service=build_ai_service,
        )
    )


async def _analyze_and_reply(
    *,
    callback,
    db: Database,
    user_id: int,
    days: int,
    build_ai_service: Callable[[AsyncSession], AIAnalysisService],
) -> None:
    """Фоновая часть: тяжёлая работа вне handler'а.

    Открывает собственную сессию — сессия запроса, в котором пришёл
    callback, к этому моменту уже закрыта DatabaseMiddleware.
    """
    period_end = datetime.now(tz=timezone.utc)
    period_start = period_end - timedelta(days=days)

    try:
        async with db.session() as session:
            trades = await load_facts(
                session, user_id=user_id, period_start=period_start, period_end=period_end
            )
            ai_service = build_ai_service(session)
            insights = await ai_service.analyze(
                user_id=user_id,
                trades=trades,
                period_start=period_start,
                period_end=period_end,
            )
        text = reports.render(insights)
    except Exception:
        logger.exception("journal analysis failed", extra={"insight_user_id": user_id})
        text = "Не удалось построить разбор. Попробуй ещё раз через пару минут."

    try:
        await callback.message.edit_text(text, reply_markup=insights_menu())
    except Exception:
        logger.exception("failed to deliver insights", extra={"insight_user_id": user_id})
