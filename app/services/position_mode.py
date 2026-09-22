"""Кэш режима позиций BingX (раздел 16 ТЗ, шаг 15.5.1).

По образцу app/services/permissions.py — тот же принцип "сбой не значит
можно", другой механизм TTL. permissions.py кэширует в колонке БД
(ExchangeCredentials.permissions_checked_at) — canonical-паттерн проекта
для такого рода данных, но здесь сознательно не мигрируем: режим позиций
не требует персистентности между рестартами бота (стоимость промаха —
один лишний GET, не потеря данных), поэтому TTL — в памяти процесса
(TTLCache), отдельным от MarketDataService кэшем: тот публичный и общий
на процесс по конструкции, а режим позиций — приватные, подписанные
данные конкретного аккаунта, смешивать архитектурно неверно независимо
от того, что сейчас пользователь один (см. CLAUDE.md).

Ключ кэша включает user_id — уже сейчас не нужно (один пользователь), но
не привязываемся к этому фактом внутри кода, который его не проверяет.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.exchanges.base import ExchangeClient, ExchangeError
from app.market.cache import TTLCache

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PositionModeOutcome:
    trustworthy: bool
    dual_side_position: bool | None
    error: ExchangeError | None = None


async def refresh_position_mode(
    cache: TTLCache, client: ExchangeClient, user_id: int, *, ttl_seconds: int
) -> PositionModeOutcome:
    """cache.get_or_fetch сам решает, бить ли по сети — TTL хранится в
    самом кэше, не здесь. Сбой (ExchangeError) не кладём в кэш: value
    записывается в TTLCache только при успешном возврате factory(), при
    исключении управление уходит в except раньше строки self.set(...)
    (см. TTLCache.get_or_fetch) — следующий вызов попробует заново, а не
    залипнет на "неизвестно" до истечения TTL."""
    key = f"{client.name}:{user_id}"
    try:
        dual_side_position = await cache.get_or_fetch(
            key, ttl_seconds, client.get_position_mode
        )
    except ExchangeError as exc:
        logger.warning(
            "Не удалось проверить режим позиций BingX",
            extra={"user_id": user_id},
        )
        return PositionModeOutcome(trustworthy=False, dual_side_position=None, error=exc)
    return PositionModeOutcome(trustworthy=True, dual_side_position=dual_side_position)
