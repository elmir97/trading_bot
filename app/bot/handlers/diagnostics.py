"""Диагностика инфраструктуры прямо из Telegram.

Полезно на VPS: не нужно лезть по SSH, чтобы понять, жива ли база.
"""

from __future__ import annotations

import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.logging import get_logger
from app.database.session import Database

router = Router(name="diagnostics")
logger = get_logger(__name__)


@router.message(Command("ping"))
async def cmd_ping(message: Message, db: Database) -> None:
    started = time.perf_counter()
    try:
        await db.healthcheck()
    except Exception:
        logger.exception("Healthcheck БД не прошёл")
        await message.answer("База данных недоступна. Подробности в логе.")
        return

    elapsed_ms = (time.perf_counter() - started) * 1000
    await message.answer(f"База данных отвечает: {elapsed_ms:.0f} мс")
