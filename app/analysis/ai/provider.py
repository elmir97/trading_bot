"""Клиент Anthropic Messages API.

Единственное место в проекте, знающее про формат запросов Anthropic.
Ключ приходит из конфига и никогда не попадает ни в лог, ни в Telegram.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.analysis.ai.base import (
    LLMAuthError,
    LLMClient,
    LLMRateLimitError,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    LLMUsage,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"
MESSAGES_PATH = "/v1/messages"
API_VERSION = "2023-06-01"

RETRY_STATUSES = frozenset({429, 500, 502, 503, 529})


class AnthropicClient(LLMClient):
    """Асинхронный клиент поверх httpx."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        auth_scheme: str = "x-api-key",
        timeout: float = 60.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = base_url.rstrip("/") + MESSAGES_PATH
        self._auth_scheme = auth_scheme
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def complete(self, *, system: str, user: str, max_tokens: int) -> LLMResponse:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            **self._auth_header(),
        }

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.post(self._url, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = LLMTimeoutError("timeout")
                logger.warning("llm timeout", extra={"llm_attempt": attempt})
            except httpx.HTTPError as exc:
                last_error = _wrap_transport_error(exc)
                logger.warning("llm transport error", extra={"llm_attempt": attempt})
            else:
                if response.status_code in (401, 403):
                    raise LLMAuthError("invalid api key")
                if response.status_code in RETRY_STATUSES:
                    last_error = LLMRateLimitError(f"status {response.status_code}")
                    logger.warning(
                        "llm retryable status",
                        extra={"llm_attempt": attempt, "llm_status": response.status_code},
                    )
                elif response.status_code >= 400:
                    raise LLMResponseError(f"status {response.status_code}")
                else:
                    return _parse_response(response.json(), fallback_model=self._model)

            if attempt < self._max_retries:
                await asyncio.sleep(2 ** (attempt - 1))

        raise last_error or LLMResponseError("unknown failure")

    def _auth_header(self) -> dict[str, str]:
        """Anthropic ждёт x-api-key, часть прокси — Authorization: Bearer."""
        if self._auth_scheme == "bearer":
            return {"Authorization": f"Bearer {self._api_key}"}
        return {"x-api-key": self._api_key}

    async def aclose(self) -> None:
        await self._client.aclose()


def _wrap_transport_error(exc: httpx.HTTPError) -> LLMResponseError:
    """Оборачивает транспортную ошибку, не утаскивая в сообщение заголовки."""
    return LLMResponseError(type(exc).__name__)


def _parse_response(data: dict, *, fallback_model: str) -> LLMResponse:
    """Извлекает текст и расход токенов из ответа Messages API."""
    blocks = data.get("content") or []
    text = "\n".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    if not text.strip():
        raise LLMResponseError("empty content")

    usage = data.get("usage") or {}
    return LLMResponse(
        text=text,
        model=data.get("model", fallback_model),
        usage=LLMUsage(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        ),
    )
