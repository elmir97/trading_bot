"""Кэш режима маржи символа BingX (28.09).

По образцу app/services/position_mode.py: приватный TTLCache в памяти
процесса, сбой не кэшируется, читается только на карточке — на «Да»
значение несётся из ExecutionQuote. Отличие одно: режим маржи у BingX —
по символу, поэтому символ входит в ключ кэша.

Зачем: плечо от стопа (app/execution/leverage.py) и проверка ликвидации
в read-back рассчитаны на изолированную маржу. При кроссе ликвидация
считается от всего счёта — гвард MARGIN_NOT_ISOLATED отказывает на
карточке, сбой чтения — MARGIN_MODE_UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.exchanges.base import ExchangeClient, ExchangeError, MarginType
from app.market.cache import TTLCache

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MarginTypeOutcome:
    trustworthy: bool
    margin_type: MarginType | None
    error: ExchangeError | None = None


async def refresh_margin_type(
    cache: TTLCache, client: ExchangeClient, user_id: int, symbol: str, *, ttl_seconds: int
) -> MarginTypeOutcome:
    key = f"{client.name}:{user_id}:{symbol}"
    try:
        margin_type = await cache.get_or_fetch(
            key, ttl_seconds, lambda: client.get_margin_type(symbol)
        )
    except ExchangeError as exc:
        logger.warning(
            "Не удалось проверить режим маржи BingX",
            extra={"user_id": user_id, "symbol": symbol},
        )
        return MarginTypeOutcome(trustworthy=False, margin_type=None, error=exc)
    return MarginTypeOutcome(trustworthy=True, margin_type=margin_type)
