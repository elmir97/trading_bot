"""Общая обвязка фоновых циклов.

Требование этапа: падение одной задачи не должно останавливать бота и не
должно останавливать другие задачи. AsyncIOScheduler и так изолирует job'ы
друг от друга, но это дополнительный явный уровень защиты и единообразное
логирование каждого цикла, а не полагание только на поведение библиотеки
по умолчанию.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

from app.core.logging import get_logger

logger = get_logger(__name__)


def fmt_decimal(value: Decimal | None) -> str:
    """Убирает незначащие нули в числе для текста уведомления.

    Локальная копия правила из app.bot.keyboards.trade.fmt_num: воркеры не
    зависят от app.bot.* (см. docstring пакета), а сама логика — три строки,
    не стоящие отдельного модуля на двоих.
    """
    if value is None:
        return "—"
    normalized = value.normalize()
    sign, digits, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return f"{normalized:f}"


def job_wrapper(name: str, func: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
    """Оборачивает job логированием начала/конца цикла и перехватом исключений.

    Исключение логируется целиком (logger.exception) и проглатывается: сам
    факт, что один цикл сканера или монитора упал, не должен ронять процесс
    бота и не должен помешать следующему тику того же job'а или другим job'ам.
    """

    async def wrapped() -> None:
        started = time.monotonic()
        # Имя job'ы — в самом тексте сообщения, а не только в extra: при
        # LOG_JSON=false (как на проде) форматтер печатает только message,
        # extra-поля в plain-режиме не видны — иначе все три задачи
        # выглядели бы в логе одинаково.
        logger.info(f"Фоновый цикл начат: {name}", extra={"job": name})
        try:
            await func()
        except Exception:
            logger.exception(f"Фоновый цикл упал: {name}", extra={"job": name})
        else:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                f"Фоновый цикл завершён: {name} ({elapsed_ms} мс)",
                extra={"job": name, "elapsed_ms": elapsed_ms},
            )

    return wrapped
