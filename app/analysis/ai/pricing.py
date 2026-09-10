"""Прайс моделей для локального учёта бюджета.

Модуль намеренно без зависимостей: его импортирует `core/config.py`
для валидации `AI_MODEL` на старте, и тянуть туда весь AI-слой нельзя.

Два профиля, потому что цены и валюта различаются:

- anthropic — прямой доступ, USD, тарифы Anthropic
- proxyapi  — api.proxyapi.ru, рубли с НДС, свой прайс

Множителя наценки нет намеренно: у прокси не процент сверху, а
собственная сетка цен. Умножать одну на другую — гарантированно врать.

Цены нужны только счётчику расходов внутри бота. Реальный счёт выставляет
провайдер. Сверять при смене тарифов: proxyapi.ru/pricing.
"""

from __future__ import annotations

from decimal import Decimal

MILLION = Decimal("1000000")

# model -> (цена за 1M входных, цена за 1M выходных)
ANTHROPIC_USD: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("3"), Decimal("15")),
    "claude-haiku-4-5-20251001": (Decimal("1"), Decimal("5")),
}

# Проверено по proxyapi.ru/pricing, цены с НДС 5%.
# claude-haiku-4-5 намеренно отсутствует: на странице тарифов его цена
# не подтверждена. Понадобится — добавь строку, взяв числа с сайта.
PROXYAPI_RUB: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal("1516"), Decimal("7579")),
    "claude-sonnet-5": (Decimal("600"), Decimal("3030")),
}

PROFILES: dict[str, dict[str, tuple[Decimal, Decimal]]] = {
    "anthropic": ANTHROPIC_USD,
    "proxyapi": PROXYAPI_RUB,
}

CURRENCY: dict[str, str] = {
    "anthropic": "USD",
    "proxyapi": "RUB",
}


class UnknownProfile(ValueError):
    """Неизвестный профиль прайса."""


class UnknownModel(ValueError):
    """Модель отсутствует в прайсе профиля — считать бюджет нечем."""


def pricing_for(profile: str) -> dict[str, tuple[Decimal, Decimal]]:
    try:
        return PROFILES[profile]
    except KeyError as exc:
        raise UnknownProfile(
            f"unknown pricing profile: {profile}. Known: {', '.join(sorted(PROFILES))}"
        ) from exc


def is_known(model: str, profile: str) -> bool:
    return model in pricing_for(profile)


def currency(profile: str) -> str:
    pricing_for(profile)
    return CURRENCY[profile]


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    profile: str = "anthropic",
) -> Decimal:
    """Стоимость вызова в валюте профиля.

    Фолбэка по цене нет намеренно. Неизвестная модель означала бы либо
    нулевой расход (лимит не сработает), либо цену наугад (учёт врёт).
    Опечатка в AI_MODEL ловится валидатором конфига при старте.
    """
    table = pricing_for(profile)
    try:
        price_in, price_out = table[model]
    except KeyError as exc:
        raise UnknownModel(
            f"no pricing for {model} in profile {profile}. "
            f"Known: {', '.join(sorted(table))}"
        ) from exc

    return (Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out) / MILLION
