"""Сервис AI-анализа журнала.

Порядок ровно такой: собрать факты → найти закономерности → проверить
кэш → проверить лимиты → сходить в модель → провалидировать → сохранить.

Любой сбой на шагах после findings приводит к отчёту без AI-текста,
а не к ошибке для пользователя: детерминированная часть готова всегда.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from app.analysis.ai.base import (
    LLMBudgetExceeded,
    LLMClient,
    LLMDisabled,
    LLMError,
    LLMResponseError,
)
from app.analysis.ai.pricing import estimate_cost
from app.analysis.ai.prompts import SYSTEM_PROMPT, build_user_message
from app.analysis.ai.schema import AIReport, parse_report
from app.analysis.insights.facts import FactPack, TradeFact, build_fact_pack
from app.analysis.insights.patterns import Finding, detect_all

logger = logging.getLogger(__name__)

# Грубая оценка: для русского текста и JSON около 3 символов на токен.
CHARS_PER_TOKEN = Decimal("3")


def estimate_tokens(text: str) -> int:
    """Оценка размера промпта до отправки — чтобы не улететь в лишний счёт."""
    return int(Decimal(len(text)) / CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class JournalInsights:
    """Результат этапа 11: цифры всегда, текст модели — если получилось."""

    pack: FactPack
    findings: list[Finding]
    ai: AIReport | None
    ai_error: str | None = None
    cached: bool = False


class AIAnalysisService:
    """Связывает детерминированный слой, LLM и хранилище отчётов."""

    def __init__(
        self,
        *,
        client: LLMClient | None,
        reports_repo,
        model: str,
        max_output_tokens: int,
        max_input_tokens: int,
        monthly_budget_usd: Decimal,
        enabled: bool,
        pricing_profile: str = "anthropic",
    ) -> None:
        self._client = client
        self._repo = reports_repo
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_input_tokens = max_input_tokens
        self._budget = monthly_budget_usd
        self._enabled = enabled
        self._profile = pricing_profile

    async def analyze(
        self,
        *,
        user_id: int,
        trades: Sequence[TradeFact],
        period_start: datetime,
        period_end: datetime,
        force: bool = False,
    ) -> JournalInsights:
        pack = build_fact_pack(trades, period_start, period_end)
        findings = detect_all(pack, trades)

        if not pack.is_sufficient or not findings:
            return JournalInsights(pack=pack, findings=findings, ai=None,
                                   ai_error="INSUFFICIENT_DATA")

        fingerprint = pack.fingerprint()

        if not force:
            cached = await self._repo.get_by_fingerprint(user_id, fingerprint)
            if cached is not None:
                return JournalInsights(
                    pack=pack,
                    findings=findings,
                    ai=AIReport.model_validate(cached.report_json),
                    cached=True,
                )

        try:
            report, usage = await self._call_model(pack, findings, user_id)
        except LLMError as exc:
            logger.warning("ai analysis failed", extra={"ai_reason": type(exc).__name__})
            return JournalInsights(
                pack=pack, findings=findings, ai=None, ai_error=type(exc).__name__
            )

        await self._repo.save(
            user_id=user_id,
            fingerprint=fingerprint,
            period_start=period_start,
            period_end=period_end,
            facts_json={"facts": pack.to_dict(), "findings": [f.to_dict() for f in findings]},
            report_json=report.model_dump(),
            model=self._model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=estimate_cost(
                self._model,
                usage.input_tokens,
                usage.output_tokens,
                profile=self._profile,
            ),
        )
        return JournalInsights(pack=pack, findings=findings, ai=report)

    async def _call_model(self, pack: FactPack, findings: list[Finding], user_id: int):
        if not self._enabled or self._client is None:
            raise LLMDisabled("ai layer disabled")

        user_message = build_user_message(pack, findings)
        prompt_tokens = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(user_message)
        if prompt_tokens > self._max_input_tokens:
            raise LLMBudgetExceeded(f"prompt too large: ~{prompt_tokens} tokens")

        spent = await self._repo.spent_this_month(user_id)
        if spent >= self._budget:
            raise LLMBudgetExceeded(f"monthly budget reached: {spent}")

        response = await self._client.complete(
            system=SYSTEM_PROMPT,
            user=user_message,
            max_tokens=self._max_output_tokens,
        )

        try:
            report = parse_report(response.text)
        except ValueError as exc:
            raise LLMResponseError(str(exc)) from exc

        allowed = {f.code for f in findings}
        return report.known_codes_only(allowed), response.usage


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
