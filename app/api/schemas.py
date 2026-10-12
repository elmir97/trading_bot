"""Ответы API. Деньги, цены, проценты — Decimal: в JSON уходят строками
(pydantic v2), уже округлёнными для показа — JS только форматирует (§5)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel

CENT = Decimal("0.01")


def money(value: Decimal | None) -> Decimal | None:
    """Деньги и проценты для показа — два знака, ROUND_HALF_UP (как formatting.py)."""
    return None if value is None else value.quantize(CENT, rounding=ROUND_HALF_UP)


class ErrorBody(BaseModel):
    code: str
    message: str


class Health(BaseModel):
    ok: bool


class Plan(BaseModel):
    risk_percent: Decimal | None      # риск по умолчанию = максимум плана
    max_leverage: int | None
    min_rr: Decimal | None
    allowed_symbols: list[str]


class Flags(BaseModel):
    dry_run: bool                     # EXEC_OPEN_DRY_RUN — на биржу не уйдёт
    live_allowed: bool                # EXEC_OPEN_ALLOW_LIVE


class Me(BaseModel):
    account_mode: str                 # DEMO | LIVE
    equity: Decimal | None
    available: Decimal | None
    balance_error: str | None         # баланс не прочитан — почему (экран: «—»)
    margin_type: str                  # ISOLATED | CROSSED — для новых сделок
    plan: Plan
    flags: Flags
    limit_expiry_minutes: list[int]
    limit_expiry_default: int
    card_ttl_seconds: int


__all__ = ["ErrorBody", "Flags", "Health", "Me", "Plan", "money"]
