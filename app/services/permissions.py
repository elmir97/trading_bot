"""Проверка прав API-ключа BingX (раздел 8 ТЗ).

is_read_only на ExchangeCredentials больше не декларация пользователя —
он заполняется реальным ответом биржи (ApiRestrictions.enable_futures).
refresh_permissions() — единственное место, которое его меняет.

TTL решает, идти ли на биржу вообще (force=False, автоматический путь
из карточки подтверждения) — если отметка ещё свежая, сети не трогаем.
Кнопка "Проверить права ключа" в настройках зовёт с force=True: ей нужен
результат сейчас, независимо от TTL.

Сбой запроса — два разных исхода, а не один "не получилось":
  - если до попытки отметка была свежей (в её пределах TTL) — старое
    значение credentials.is_read_only не хуже нового, работаем по нему;
  - если отметки не было или она уже протухла — довериться нечему,
    outcome.trustworthy=False. Вызывающий код (карточка подтверждения)
    обязан отказать отдельным кодом (PERMISSIONS_UNKNOWN), а не молчать
    и не считать, что торговать можно.
Кнопке в настройках trustworthy не подходит вовсе: пользователь явно
просил проверить СЕЙЧАС, и сбой обязан быть виден ей всегда, даже если
старое значение формально ещё в пределах TTL — иначе нажатие "Проверить"
при живом сбое биржи выглядело бы как успешная проверка.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models.credentials import ExchangeCredentials
from app.exchanges.base import ExchangeClient, ExchangeError

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PermissionRefreshOutcome:
    attempted: bool                # был ли реальный поход на биржу
    trustworthy: bool              # можно ли доверять credentials.is_read_only прямо сейчас
    error: ExchangeError | None = None


async def refresh_permissions(
    session: AsyncSession,
    credentials: ExchangeCredentials,
    client: ExchangeClient,
    *,
    ttl_hours: int,
    force: bool = False,
    now: datetime | None = None,
) -> PermissionRefreshOutcome:
    moment = now or datetime.now(UTC)
    checked_at = credentials.permissions_checked_at
    is_stale = checked_at is None or moment - checked_at > timedelta(hours=ttl_hours)

    if not force and not is_stale:
        return PermissionRefreshOutcome(attempted=False, trustworthy=True)

    try:
        restrictions = await client.get_api_restrictions()
    except ExchangeError as exc:
        logger.warning(
            "Не удалось проверить права ключа BingX",
            extra={"user_id": credentials.user_id, "mode": credentials.mode.value},
        )
        # force=True и отметка была свежей — сбой не делает старое значение
        # хуже, чем оно было секунду назад. Стейл/пустая отметка — доверять
        # нечему в любом случае, force тут уже ничего не меняет.
        return PermissionRefreshOutcome(attempted=True, trustworthy=not is_stale, error=exc)

    credentials.is_read_only = not restrictions.enable_futures
    credentials.permissions_checked_at = moment
    await session.flush()
    return PermissionRefreshOutcome(attempted=True, trustworthy=True)
