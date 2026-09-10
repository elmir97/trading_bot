"""Отказ во входе — общий тип для sizing.py и guards.py (этап 15.3).

OrderRequest и OrderResult (раздел 1 ТЗ) появятся вместе с service.py,
когда будет что ими описывать — заводить их сейчас означало бы датаклассы
без единого потребителя.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExecutionRefusalCode(StrEnum):
    """Раздел 7 ТЗ. Значение — порядок проверки в run_guards()."""

    EXECUTION_DISABLED = "EXECUTION_DISABLED"
    NO_TRADING_KEY = "NO_TRADING_KEY"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    SIGNAL_ALREADY_USED = "SIGNAL_ALREADY_USED"
    POSITION_EXISTS = "POSITION_EXISTS"
    MAX_POSITIONS = "MAX_POSITIONS"
    MAX_TOTAL_RISK = "MAX_TOTAL_RISK"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    PRICE_DRIFT = "PRICE_DRIFT"
    INVALID_LEVELS = "INVALID_LEVELS"
    SIZE_TOO_SMALL = "SIZE_TOO_SMALL"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"


@dataclass(frozen=True, slots=True)
class ExecutionRefusal:
    code: ExecutionRefusalCode
    message: str
