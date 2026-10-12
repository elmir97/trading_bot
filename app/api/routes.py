"""Эндпоинты Mini App (docs/miniapp-plan.md §3)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, Session, State
from app.api.schemas import Flags, Health, Me, Plan, money
from app.core.logging import get_logger
from app.exchanges.base import ExchangeError
from app.execution.opening.service import (
    LIMIT_EXPIRY_CHOICES,
    desired_margin_type,
    plan_limits,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api")


@router.get("/health")
async def health() -> Health:
    """Без авторизации и без данных пользователя — проверка деплоя."""
    return Health(ok=True)


@router.get("/me")
async def me(user: CurrentUser, state: State, session: Session) -> Me:
    settings = state.settings
    mode = settings.bingx_allowed_exchange_mode
    limits = await plan_limits(session, user.id)
    equity = available = None
    balance_error: str | None = None
    creds = await state.factory.get_credentials(session, user.id, mode=mode)
    if creds is None:
        balance_error = "Ключ BingX не подключён — подключи в настройках бота."
    else:
        client = await state.factory.for_user(session, user.id, mode=mode)
        try:
            balance = await client.get_balance()
            equity, available = balance.equity, balance.available
        except ExchangeError as exc:
            logger.warning("Mini App: баланс не прочитан",
                           extra={"user_id": user.id, "error": type(exc).__name__})
            balance_error = f"Биржа не ответила ({type(exc).__name__}) — обнови."
        finally:
            await client.close()
    return Me(
        account_mode=mode.value,
        equity=money(equity),
        available=money(available),
        balance_error=balance_error,
        margin_type=desired_margin_type(user).value,
        plan=Plan(
            risk_percent=money(limits.risk_percent), max_leverage=limits.max_leverage,
            min_rr=money(limits.min_rr), allowed_symbols=list(limits.allowed_symbols),
        ),
        flags=Flags(dry_run=settings.exec_open_dry_run,
                    live_allowed=settings.exec_open_allow_live),
        limit_expiry_minutes=list(LIMIT_EXPIRY_CHOICES),
        limit_expiry_default=settings.exec_open_limit_expiry_minutes,
        card_ttl_seconds=settings.exec_open_card_ttl_seconds,
    )


__all__ = ["router"]
