"""Дневная сводка, алерт дневного лимита убытка (этап 12) и сводка
исполнения (этап 15.4, раздел 12а).

Все три уведомления — не чаще одного раза в локальный календарный день
пользователя: дата последней отправки хранится в user_settings и
сравнивается с "сегодня" по местному времени (тот же tz_offset_for, что и
в интерактивных отчётах — см. app/trading/risk.py). Это гейтинг отправки —
когда слать, не про что слать.

Окно данных — другое дело, и у сводки исполнения (раздел 12а) оно не
совпадает с двумя остальными: час отправки (EXEC_DAILY_DIGEST_HOUR) не
обязан быть локальной полночью, поэтому day_bounds (календарные сутки)
резал бы события между часом отправки и полночью — они не попадали бы ни
в сегодняшнюю сводку (её уже нет), ни в завтрашнюю (окно уже следующего
дня). Поэтому у сводки исполнения окно — скользящие 24 часа до момента
отправки, не day_bounds. У дневной сводки сделок и алерта лимита убытка
окно осталось day_bounds — календарный день им и нужен.

Проверка лимита убытка требует текущего баланса в процентах — как и
PlanValidator при сохранении сделки, взять его неоткуда, кроме биржи:
процент риска без баланса не посчитать. Поэтому алерт молча пропускается
для пользователей без подключённых ключей — то же самое ограничение уже
есть в PlanValidator.check().

Сводка исполнения на биржу не ходит: она считает уже накопленные строки
execution_orders и signals (см. app/workers/execution_digest.py), поэтому
доступна даже пользователям без подключённых ключей.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import Bot

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.user import User, UserSettings
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.database.repositories.signal_notification import SignalNotificationRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ExchangeAuthError, ExchangeError
from app.services.exchange_factory import ExchangeFactory
from app.services.statistics_service import StatisticsService
from app.trading.risk import day_bounds, tz_offset_for
from app.trading.statistics import Statistics, calculate_statistics
from app.workers.base import fmt_decimal
from app.workers.execution_digest import build_stats, render_execution_digest
from app.workers.notifier import notification_enabled, send_notification
from app.workers.scanner import SetupScanner

logger = get_logger(__name__)

ZERO = Decimal(0)


def render_daily_summary(stats: Statistics) -> str:
    if stats.total_trades == 0:
        return "<b>Итоги дня</b>\n\nСегодня сделок не было."

    lines = [
        "<b>Итоги дня</b>",
        "",
        f"Сделок: {stats.total_trades} · прибыльных: {stats.wins} · "
        f"убыточных: {stats.losses}",
        f"Win Rate: {fmt_decimal(stats.win_rate)}%",
        f"PnL: {fmt_decimal(stats.total_pnl)} USDT",
    ]
    if stats.average_rr is not None:
        lines.append(f"Средний R: {fmt_decimal(stats.average_rr)}")
    return "\n".join(lines)


class DailyJobs:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        settings: Settings,
        cipher: SecretCipher,
        *,
        scanner: SetupScanner | None = None,
    ) -> None:
        self._bot = bot
        self._db = db
        self._settings = settings
        self._exchange_factory = ExchangeFactory(settings, cipher)
        # Раздел "троттлинг сканера": последний замер run() для строки в
        # сводке исполнения. None в тестах/там, где сводка исполнения не
        # нужна — не обязателен для работы остальных двух уведомлений.
        self._scanner = scanner

    async def run(self) -> None:
        now = datetime.now(UTC)
        async with self._db.session() as session:
            users = await UserRepository(session).list_active_with_plan()
            for user in users:
                try:
                    await self._process_user(session, user, now)
                except Exception:
                    logger.exception(
                        "Дневная проверка пользователя упала", extra={"user_id": user.id}
                    )
            await session.flush()

    async def _process_user(self, session, user: User, now: datetime) -> None:
        settings_row = user.settings
        if settings_row is None or user.trading_plan is None:
            return

        tz_offset = tz_offset_for(settings_row.timezone)
        today_local = (now + timedelta(hours=tz_offset)).date()
        local_hour = (now + timedelta(hours=tz_offset)).hour

        await self._maybe_send_summary(session, user, settings_row, now, tz_offset, today_local, local_hour)
        await self._maybe_send_loss_alert(session, user, settings_row, now, tz_offset, today_local)
        await self._maybe_send_execution_digest(
            session, user, settings_row, now, today_local, local_hour
        )

    async def _maybe_send_summary(
        self,
        session,
        user: User,
        settings_row: UserSettings,
        now: datetime,
        tz_offset: int,
        today_local,
        local_hour: int,
    ) -> None:
        if not notification_enabled(settings_row, "daily_report"):
            return
        if settings_row.daily_summary_last_sent_date == today_local:
            return
        if local_hour < self._settings.daily_summary_hour_local:
            return

        day_start, day_end = day_bounds(now, tz_offset)
        service = StatisticsService(session)
        snapshots = await service.load_snapshots(user.id, start=day_start, end=day_end)
        equity = await service.starting_equity(user.id)
        stats = calculate_statistics(snapshots, starting_equity=equity)

        settings_row.daily_summary_last_sent_date = today_local
        await send_notification(self._bot, user.telegram_id, render_daily_summary(stats))

    async def _maybe_send_loss_alert(
        self,
        session,
        user: User,
        settings_row: UserSettings,
        now: datetime,
        tz_offset: int,
        today_local,
    ) -> None:
        if not notification_enabled(settings_row, "daily_limit_reached"):
            return
        if settings_row.daily_loss_alert_last_sent_date == today_local:
            return

        plan = user.trading_plan
        try:
            # Этап 15.4в: баланс для этого алерта — то же самое "читает и
            # показывает", что и остальной интерфейс, поэтому берётся для
            # счёта, выбранного в настройках, а не для счёта исполнения.
            client = await self._exchange_factory.for_user(
                session, user.id, mode=settings_row.active_exchange_mode
            )
        except ExchangeAuthError:
            return  # ключи не подключены — процент риска не посчитать

        try:
            balance = (await client.get_balance()).equity
        except ExchangeError:
            logger.warning("Баланс недоступен для дневного алерта", extra={"user_id": user.id})
            return
        finally:
            await client.close()

        if balance <= ZERO:
            return

        day_start, day_end = day_bounds(now, tz_offset)
        day_pnl = await TradeRepository(session).sum_pnl_between(user.id, day_start, day_end)
        if day_pnl is None:
            return

        day_loss_pct = -day_pnl / balance * Decimal(100)
        if day_loss_pct < plan.max_daily_loss_percent:
            return

        settings_row.daily_loss_alert_last_sent_date = today_local
        text = (
            f"🛑 <b>Дневной лимит убытка достигнут</b>\n\n"
            f"Убыток за день: −{fmt_decimal(day_loss_pct)}% при лимите "
            f"{fmt_decimal(plan.max_daily_loss_percent)}%.\n\n"
            f"Методология рекомендует закрыть торговый день."
        )
        await send_notification(self._bot, user.telegram_id, text)

    async def _maybe_send_execution_digest(
        self,
        session,
        user: User,
        settings_row: UserSettings,
        now: datetime,
        today_local,
        local_hour: int,
    ) -> None:
        """Раздел 12а ТЗ. Час свой (EXEC_DAILY_DIGEST_HOUR), не
        DAILY_SUMMARY_HOUR_LOCAL — переключатель у пользователя отдельный
        ("🔔 Уведомления" → "сводка исполнения"), поэтому и час не завязан
        на обычную дневную сводку.

        Окно данных — скользящие 24 часа до now, не day_bounds: час отправки
        почти никогда не совпадает с локальной полночью, и календарные сутки
        резали бы события между часом отправки и полночью — они не попадали
        бы ни в сегодняшнюю сводку, ни в завтрашнюю (см. докстринг модуля).
        tz_offset этому методу не нужен — гейтинг "не чаще раза в локальный
        день" (today_local/local_hour) уже посчитан вызывающим _process_user,
        а для самого окна часовой пояс не имеет значения: это фиксированная
        длительность, а не "начало суток по местному времени".
        """
        if not notification_enabled(settings_row, "execution_digest"):
            return
        if settings_row.execution_digest_last_sent_date == today_local:
            return
        if local_hour < self._settings.exec_daily_digest_hour:
            return

        window_start = now - timedelta(hours=24)
        rows = await ExecutionOrderRepository(session).list_entries_between(
            user.id, window_start, now
        )
        # Отдельный источник (signals, не execution_orders) для "Сигналов
        # READY" — то же окно window_start..now, что и у rows выше (раздел
        # 12а, execution_digest.py).
        ready_signals = await SignalNotificationRepository(session).count_ready_between(
            user.id, window_start, now
        )
        unprotected = await ExecutionOrderRepository(session).list_unprotected_between(
            user.id, window_start, now
        )
        plan = user.trading_plan
        target_risk_percent = plan.risk_per_trade_percent if plan else None
        stats = build_stats(
            rows,
            target_risk_percent=target_risk_percent,
            ready_signals=ready_signals,
            unprotected=unprotected,
        )

        settings_row.execution_digest_last_sent_date = today_local
        scan_cycle = self._scanner.last_cycle if self._scanner else None
        text = render_execution_digest(
            stats,
            max_price_drift_ratio=self._settings.exec_max_price_drift_ratio,
            scan_cycle=scan_cycle,
        )
        await send_notification(self._bot, user.telegram_id, text)
