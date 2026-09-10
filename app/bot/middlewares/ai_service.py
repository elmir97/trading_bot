"""Внедряет AI-сервис разбора журнала в каждый апдейт.

Сервис собирается на каждый запрос вместе с сессией — репозиторий должен
жить в той же транзакции, что и остальной хендлер (см. DatabaseMiddleware).

Отдельно прокидывается billder — фабрика, принимающая сессию и
возвращающая новый AIAnalysisService. DatabaseMiddleware закрывает свою
сессию сразу по выходу из handler'а, а фоновая asyncio-задача разбора
журнала переживает handler и должна открыть собственную сессию — фабрика
как раз позволяет собрать сервис заново на этой новой сессии.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.ai.base import LLMClient
from app.analysis.ai.service import AIAnalysisService
from app.core.config import Settings
from app.database.repositories.ai_report import AIReportRepository


class AIServiceMiddleware(BaseMiddleware):
    """Кладёт в data готовый `ai_service` и фабрику `build_ai_service`."""

    def __init__(self, llm_client: LLMClient | None, settings: Settings) -> None:
        self._llm_client = llm_client
        self._settings = settings

    def _build(self, session: AsyncSession) -> AIAnalysisService:
        settings = self._settings
        return AIAnalysisService(
            client=self._llm_client,
            reports_repo=AIReportRepository(session),
            model=settings.ai_model,
            max_output_tokens=settings.ai_max_output_tokens,
            max_input_tokens=settings.ai_max_input_tokens,
            monthly_budget_usd=settings.ai_monthly_budget_usd,
            enabled=settings.ai_enabled,
            pricing_profile=settings.ai_pricing_profile,
        )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["build_ai_service"] = self._build
        session: AsyncSession | None = data.get("session")
        if session is not None:
            data["ai_service"] = self._build(session)
        return await handler(event, data)
