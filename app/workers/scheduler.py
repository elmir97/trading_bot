"""Собирает фоновые задачи в один APScheduler внутри процесса бота.

Один AsyncIOScheduler на все три задачи, а не три отдельных цикла: они и
так работают в общем event loop процесса (требование 1 — без отдельного
контейнера), а планировщик уже даёт нужную изоляцию между job'ами —
падение одной не трогает расписание остальных.
"""

from __future__ import annotations

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.session import Database
from app.workers.base import job_wrapper
from app.workers.daily import DailyJobs
from app.workers.positions import PositionMonitor
from app.workers.scanner import SetupScanner

logger = get_logger(__name__)


class BackgroundJobs:
    def __init__(
        self, bot: Bot, db: Database, settings: Settings, cipher: SecretCipher
    ) -> None:
        self._settings = settings
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scanner = SetupScanner(bot, db, settings)
        self._positions = PositionMonitor(bot, db, settings)
        self._daily = DailyJobs(bot, db, settings, cipher, scanner=self._scanner)

    def start(self) -> None:
        """Требование 8: пока BACKGROUND_JOBS_ENABLED=false — не регистрирует
        и не запускает ни одной job'ы, а не просто не шлёт уведомления."""
        if not self._settings.background_jobs_enabled:
            logger.info("Фоновые задачи выключены (BACKGROUND_JOBS_ENABLED=false)")
            return

        self._scheduler.add_job(
            job_wrapper("setup_scanner", self._scanner.run),
            "interval",
            minutes=self._settings.setup_scanner_interval_minutes,
            id="setup_scanner",
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            job_wrapper("position_monitor", self._positions.run),
            "interval",
            minutes=self._settings.position_monitor_interval_minutes,
            id="position_monitor",
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
                "setup_scanner_minutes": self._settings.setup_scanner_interval_minutes,
                "position_monitor_minutes": self._settings.position_monitor_interval_minutes,
                "daily_jobs_minutes": self._settings.daily_jobs_interval_minutes,
            },
        )

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Фоновые задачи остановлены")
