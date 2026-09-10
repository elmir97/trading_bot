"""Абстрактный LLM-клиент и ошибки AI-слоя.

Бизнес-логика зависит от этого интерфейса, а не от Anthropic. Смена
провайдера — это новая реализация `LLMClient`, а не правка сервиса.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMError(Exception):
    """Базовая ошибка AI-слоя. Наверх пользователю не показывается."""


class LLMTimeoutError(LLMError):
    """Провайдер не ответил вовремя."""


class LLMRateLimitError(LLMError):
    """Превышен лимит запросов провайдера."""


class LLMAuthError(LLMError):
    """Неверный или отсутствующий ключ."""


class LLMResponseError(LLMError):
    """Ответ получен, но не разобран или не прошёл валидацию схемы."""


class LLMBudgetExceeded(LLMError):
    """Исчерпан месячный лимит расходов, заданный в конфиге."""


class LLMDisabled(LLMError):
    """AI-слой выключен в настройках."""


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Расход токенов одного вызова — пишется в БД для учёта бюджета."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    usage: LLMUsage


class LLMClient(ABC):
    """Минимальный контракт: системный промпт + пользовательский → текст."""

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
    ) -> LLMResponse:
        """Один запрос к модели. Бросает подклассы LLMError."""

    @abstractmethod
    async def aclose(self) -> None:
        """Закрывает HTTP-сессию."""
