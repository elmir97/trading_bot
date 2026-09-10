"""Схема ответа LLM.

Модель обязана вернуть JSON этой формы. Всё, что не проходит валидацию,
считается сбоем провайдера — отчёт откатывается на детерминированный.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class AIInsight(BaseModel):
    """Один вывод модели, привязанный к конкретному finding."""

    finding_code: str = Field(max_length=64)
    title: str = Field(max_length=120)
    explanation: str = Field(max_length=600)
    action: str = Field(max_length=300)


class AIReport(BaseModel):
    """Полный разбор журнала."""

    status: Literal["OK", "INSUFFICIENT_DATA"]
    summary: str = Field(default="", max_length=800)
    insights: list[AIInsight] = Field(default_factory=list, max_length=6)
    watch_next: list[str] = Field(default_factory=list, max_length=4)

    def known_codes_only(self, allowed: set[str]) -> "AIReport":
        """Отбрасывает выводы, ссылающиеся на несуществующие findings.

        Защита от того, что модель придумает закономерность, которой в
        фактах не было.
        """
        filtered = [i for i in self.insights if i.finding_code in allowed]
        return self.model_copy(update={"insights": filtered})


def parse_report(raw: str) -> AIReport:
    """Разбирает ответ модели, снимая markdown-обёртку, если она есть."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    try:
        return AIReport.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid AI report: {exc}") from exc
