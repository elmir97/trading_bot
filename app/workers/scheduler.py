"""Собирает фоновые задачи в один APScheduler внутри процесса бота.

Один AsyncIOScheduler на все задачи, а не отдельные циклы: они и
так работают в общем event loop процесса (требование 1 — без отдельного
контейнера), а планировщик уже даёт нужную изоляцию между job'ами —
падение одной не трогает расписание остальных.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.session import Database
from app.workers.base import job_wrapper
from app.workers.daily import DailyJobs
from app.workers.positions import PositionMonitor
from app.workers.reconciler import Reconciler

logger = get_logger(__name__)


class BackgroundJobs:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        settings: Settings,
        cipher: SecretCipher,
        redis: Any = None,
    ) -> None:
        self._settings = settings
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        # Этап 5: позиции биржи (ключи пользователя) и пропуск цикла, пока
        # жив лок действия с позицией.
        self._positions = PositionMonitor(bot, db, settings, cipher, redis)
        # Шаг 15.6: сверка журнала с биржей. Redis — чтобы пропускать цикл,
        # пока жив лок «Да» (вход в полёте).
        self._reconciler = Reconciler(bot, db, settings, cipher, redis)
        self._daily = DailyJobs(bot, db, settings, cipher, reconciler=self._reconciler)

    def start(self) -> None:
        """Требование 8: пока BACKGROUND_JOBS_ENABLED=false — не регистрирует
        и не запускает ни одной job'ы, а не просто не шлёт уведомления."""
        if not self._settings.background_jobs_enabled:
            logger.info("Фоновые задачи выключены (BACKGROUND_JOBS_ENABLED=false)")
            return

        self._scheduler.add_job(
            # quiet: цикл раз в 15 с; на INFO монитор пишет сам — уведомления
            # и пульс раз в час.
            job_wrapper("position_monitor", self._positions.run, quiet=True),
            "interval",
            seconds=self._settings.position_monitor_price_seconds,
            id="position_monitor",
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            job_wrapper("reconciler", self._reconciler.run, quiet=True),
            "interval",
            seconds=self._settings.reconciler_interval_seconds,
            id="reconciler",
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            job_wrapper("daily_jobs", self._daily.run),
            "interval",
            minutes=self._settings.daily_jobs_interval_minutes,
            id="daily_jobs",
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.start()
        logger.info(
            "Фоновые задачи запущены",
            extra={
                "position_monitor_seconds": self._settings.position_monitor_price_seconds,
                "daily_jobs_minutes": self._settings.daily_jobs_interval_minutes,
                "reconciler_seconds": self._settings.reconciler_interval_seconds,
            },
        )

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Фоновые задачи остановлены")
