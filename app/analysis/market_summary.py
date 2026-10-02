"""Пересказ цифр «Анализа рынка» моделью (этап 2) — опционально.

Цифры экрана считает код (app/analysis/market_overview.py); модель только
пересказывает их 2–3 предложениями. Проверка ответа строгая:
- каждое число в тексте модели обязано встречаться в переданных цифрах;
- слов направления сделки и советов входа нет;
- не длиннее SUMMARY_MAX_CHARS.
Не прошло, модель недоступна, бюджет исчерпан или флаг выключен — пересказа
нет, экран показывает только цифры (детерминированный фолбэк).

Учёт расходов — общий с разбором журнала: строка в ai_reports с fingerprint
"market:…" (бюджет ai_monthly_budget_usd считается по всем строкам).
Повтор тех же цифр берётся из ai_reports без вызова модели.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.analysis.ai.base import LLMClient, LLMError
from app.analysis.ai.pricing import estimate_cost
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

FINGERPRINT_PREFIX = "market:"
SUMMARY_MAX_CHARS = 400

SYSTEM_PROMPT = (
    "Ты пересказываешь техническую картину рынка по готовым цифрам. Пиши по-русски "
    "2–3 коротких предложения: что показывают тренд относительно EMA, структура, RSI, "
    "волатильность (ATR), объём, ближайшие уровни, funding и open interest. "
    "Используй только числа из входных данных и не вычисляй новых. Не давай советов, "
    "не называй направление сделки, не пиши про вход, покупку, продажу, лонг или шорт, "
    "не делай прогнозов. Без markdown и списков."
)

# Направление сделки и советы — пересказ с ними отбрасывается целиком.
_FORBIDDEN = re.compile(
    r"покуп|продав|продаж|вход|войти|лонг|шорт|long|short|buy|sell|рекоменд|совет"
    r"|стоит\s+(открыть|взять|купить)|прогноз",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_TAG = re.compile(r"<[^>]+>")
# Обратный отсчёт до funding меняется каждую минуту — в отпечаток не входит,
# иначе кэш пересказа не срабатывал бы никогда.
_COUNTDOWN = re.compile(r"следующее через [^\n]*|начисление сейчас")


def plain(text: str) -> str:
    """Текст экрана без HTML — то, что получает модель."""
    return html.unescape(_TAG.sub("", text))


def _numbers(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for raw in _NUMBER.findall(text):
        try:
            out.add(Decimal(raw.replace(",", ".")).normalize())
        except InvalidOperation:
            continue
    return out


def fingerprint(facts_text: str) -> str:
    stable = _COUNTDOWN.sub("", facts_text)
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    return FINGERPRINT_PREFIX + digest[: 64 - len(FINGERPRINT_PREFIX)]


def validate_summary(summary: str, facts_text: str) -> str | None:
    """None — пересказ годится; иначе причина отказа (для лога)."""
    if not summary.strip():
        return "empty"
    if len(summary) > SUMMARY_MAX_CHARS:
        return "too_long"
    if _FORBIDDEN.search(summary):
        return "forbidden_word"
    extra = _numbers(summary) - _numbers(facts_text)
    if extra:
        return "unknown_number"
    return None


class MarketSummaryService:
    def __init__(self, *, client: LLMClient | None, reports_repo, settings: Settings) -> None:  # type: ignore[no-untyped-def]
        self._client = client
        self._repo = reports_repo
        self._settings = settings

    @property
    def enabled(self) -> bool:
        s = self._settings
        return bool(s.ai_enabled and s.ai_market_summary_enabled and self._client is not None)

    async def summarize(self, user_id: int, screen_text: str) -> str | None:
        """Пересказ или None. Не бросает: экран не должен падать из-за модели."""
        if not self.enabled:
            return None
        facts = plain(screen_text)
        key = fingerprint(facts)
        try:
            cached = await self._repo.get_by_fingerprint(user_id, key)
            if cached is not None:
                return (cached.report_json or {}).get("summary")
            if await self._repo.spent_this_month(user_id) >= self._settings.ai_monthly_budget_usd:
                logger.info("Пересказ рынка: бюджет AI исчерпан")
                return None
            assert self._client is not None
            response = await self._client.complete(
                system=SYSTEM_PROMPT, user=facts,
                max_tokens=self._settings.ai_market_summary_max_tokens,
            )
        except LLMError as exc:
            logger.warning("Пересказ рынка не получен", extra={"ai_reason": type(exc).__name__})
            return None

        summary = " ".join(response.text.split())
        rejected = validate_summary(summary, facts)
        now = datetime.now(UTC)
        # Расход записывается и у отброшенного пересказа: токены потрачены.
        await self._repo.save(
            user_id=user_id,
            fingerprint=key,
            period_start=now,
            period_end=now,
            facts_json={"text": facts},
            report_json={"summary": None if rejected else summary, "rejected": rejected},
            model=self._settings.ai_model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=estimate_cost(
                self._settings.ai_model,
                response.usage.input_tokens,
                response.usage.output_tokens,
                profile=self._settings.ai_pricing_profile,
            ),
        )
        if rejected:
            logger.warning("Пересказ рынка отброшен", extra={"reason": rejected})
            return None
        return summary
