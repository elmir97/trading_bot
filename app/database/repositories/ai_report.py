"""Репозиторий AI-отчётов.

Все запросы фильтруются по user_id — как и везде в проекте, доступ к
чужим данным исключён на уровне репозитория, а не handler'а.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.ai_report import AIReport


class AIReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_fingerprint(self, user_id: int, fingerprint: str) -> AIReport | None:
        stmt = select(AIReport).where(
            AIReport.user_id == user_id,
            AIReport.fingerprint == fingerprint,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def last_created_at(self, user_id: int) -> datetime | None:
        stmt = select(func.max(AIReport.created_at)).where(AIReport.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one()

    async def spent_this_month(self, user_id: int) -> Decimal:
        """Сумма расходов с первого числа текущего месяца по UTC."""
        now = datetime.now(tz=timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        stmt = select(func.coalesce(func.sum(AIReport.cost_usd), 0)).where(
            AIReport.user_id == user_id,
            AIReport.created_at >= start,
        )
        return Decimal((await self._session.execute(stmt)).scalar_one())

    async def save(
        self,
        *,
        user_id: int,
        fingerprint: str,
        period_start: datetime,
        period_end: datetime,
        facts_json: dict,
        report_json: dict,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Decimal,
    ) -> AIReport:
        report = AIReport(
            user_id=user_id,
            fingerprint=fingerprint,
            period_start=period_start,
            period_end=period_end,
            facts_json=facts_json,
            report_json=report_json,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
        self._session.add(report)
        await self._session.flush()
        return report
