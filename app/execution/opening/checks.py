"""Нарушения торгового плана при открытии на бирже (решение владельца 05.10).

Блокируют (кнопки «Открыть» нет): нет стопа, риск выше максимума плана,
дневной/недельный лимит убытка достигнут (жёсткий блок — не «блок с
подтверждением», как в мастере журнала), плечо выше max_leverage плана.
Предупреждают (кнопка «Открыть всё равно»): RR ниже минимального, монеты нет в
белом списке, лимит сделок в день.

Риск и стоп считает calc (риск с комиссией, стоп обязателен): RISK_TOO_HIGH и
NO_STOP_LOSS проверки плана здесь отбрасываются, чтобы не дублировать.
Таймфрейма у открытия нет.
"""

from __future__ import annotations

from app.execution.opening.calc import Issue, Level
from app.trading.risk import PlanCheck, ViolationCode

_LEVELS: dict[ViolationCode, Level | None] = {
    ViolationCode.NO_STOP_LOSS: None,          # стоп обязателен в calc
    ViolationCode.RISK_TOO_HIGH: None,         # риск с комиссией — в calc
    ViolationCode.TIMEFRAME_NOT_ALLOWED: None,  # у открытия нет таймфрейма
    ViolationCode.DAILY_LOSS_LIMIT: Level.BLOCK,
    ViolationCode.WEEKLY_LOSS_LIMIT: Level.BLOCK,
    ViolationCode.LEVERAGE_TOO_HIGH: Level.BLOCK,
    ViolationCode.LOW_RISK_REWARD: Level.WARN,
    ViolationCode.SYMBOL_NOT_ALLOWED: Level.WARN,
    ViolationCode.DAILY_TRADE_LIMIT: Level.WARN,
}


def classify(plan_check: PlanCheck) -> list[Issue]:
    issues: list[Issue] = []
    for violation in plan_check.violations:
        level = _LEVELS[violation.code]
        if level is not None:
            issues.append(Issue(f"PLAN_{violation.code.value}", level, violation.message))
    return issues


def merge(technical: tuple[Issue, ...], plan: list[Issue]) -> tuple[Issue, ...]:
    """Блоки первыми, без повторов кода (плечо выше плана может прийти и из
    calc-подсказки, и из плана — показываем один раз)."""
    seen: set[str] = set()
    out: list[Issue] = []
    for issue in sorted((*technical, *plan), key=lambda i: 0 if i.level is Level.BLOCK else 1):
        if issue.code in seen:
            continue
        seen.add(issue.code)
        out.append(issue)
    return tuple(out)
