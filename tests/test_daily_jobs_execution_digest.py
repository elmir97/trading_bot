"""Тесты DailyJobs._maybe_send_execution_digest (этап 15.4, раздел 12а ТЗ).

Против настоящей БД (User/UserSettings/ReconciliationEvent), Bot — свой
минимальный дублёр (как FakeBot в tests/test_notifier.py), в Telegram ничего
не уходит. Проверяется склейка DailyJobs с execution_digest и три её условия
отправки (переключатель, "уже отправляли сегодня", час) — подсчёты покрыты
tests/test_execution_digest.py.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.core.security import SecretCipher
from app.database.models.reconciliation_event import ReconciliationEvent
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import ReconciliationKind
from app.trading.risk import tz_offset_for
from app.workers.daily import DailyJobs
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


class FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent_messages.append((chat_id, text))


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        )
        user = await user_service.get_or_create(telegram_id=unique_telegram_id())
        bot = FakeBot()
        cipher = SecretCipher(settings.encryption_key.get_secret_value())
        daily = DailyJobs(bot, db, settings, cipher)
        yield daily, session, user, bot, settings
        await cleanup_user(session, user)
    await db.dispose()


def _call_args(user, settings: Settings, *, local_hour: int):
    now = datetime.now(UTC)
    tz_offset = tz_offset_for(user.settings.timezone)
    today_local = (now + timedelta(hours=tz_offset)).date()
    return now, tz_offset, today_local, local_hour


def _call_args(user, settings: Settings, *, local_hour: int):
    now = datetime.now(UTC)
    tz_offset = tz_offset_for(user.settings.timezone)
    today_local = (now + timedelta(hours=tz_offset)).date()
    return now, tz_offset, today_local, local_hour


async def test_window_is_rolling_24h_not_calendar_day(ctx) -> None:  # type: ignore[no-untyped-def]
    """Окно — строго "последние 24 часа до now": событие 23 часа назад
    видно, 25 часов назад — нет, независимо от местной полуночи."""
    daily, session, user, bot, settings = ctx
    now = datetime.now(UTC)
    for hours, kind in ((23, ReconciliationKind.CLOSED_STOP_LOSS),
                        (25, ReconciliationKind.CLOSED_TAKE_PROFIT)):
        session.add(ReconciliationEvent(
            user_id=user.id, symbol="LINK-USDT", kind=kind, dedup_key=f"w:{hours}",
            detail="d", notified_at=now, created_at=now - timedelta(hours=hours),
        ))
    await session.flush()

    _now, _tz_offset, today_local, local_hour = _call_args(
        user, settings, local_hour=settings.exec_daily_digest_hour
    )
    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today_local, local_hour
    )

    text = bot.sent_messages[0][1]
    assert "Сверка с биржей: закрыто по стопу — 1" in text
    assert "по тейку" not in text


async def test_empty_day_still_sends_digest(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 12а ТЗ: "пустая строка тут не годится" — нулевой день тоже
    шлёт сводку, а не молчит."""
    daily, session, user, bot, settings = ctx

    now, _tz_offset, today_local, local_hour = _call_args(
        user, settings, local_hour=settings.exec_daily_digest_hour
    )
    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today_local, local_hour
    )

    assert len(bot.sent_messages) == 1
    text = bot.sent_messages[0][1]
    assert "Аномалии: нет" in text


async def test_skips_before_configured_hour(ctx) -> None:  # type: ignore[no-untyped-def]
    daily, session, user, bot, settings = ctx
    now, _tz_offset, today_local, _ = _call_args(user, settings, local_hour=0)
    local_hour = settings.exec_daily_digest_hour - 1

    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today_local, local_hour
    )

    assert bot.sent_messages == []
    assert user.settings.execution_digest_last_sent_date is None


async def test_skips_when_already_sent_today(ctx) -> None:  # type: ignore[no-untyped-def]
    daily, session, user, bot, settings = ctx
    now, _tz_offset, today_local, local_hour = _call_args(
        user, settings, local_hour=settings.exec_daily_digest_hour
    )
    user.settings.execution_digest_last_sent_date = today_local

    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today_local, local_hour
    )

    assert bot.sent_messages == []


async def test_respects_notification_toggle(ctx) -> None:  # type: ignore[no-untyped-def]
    """Отдельный переключатель "🔔 Уведомления" → сводка исполнения
    (app/bot/handlers/settings.py: NOTIFICATION_ORDER)."""
    daily, session, user, bot, settings = ctx
    user.settings.notifications = {**user.settings.notifications, "execution_digest": False}

    now, _tz_offset, today_local, local_hour = _call_args(
        user, settings, local_hour=settings.exec_daily_digest_hour
    )
    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today_local, local_hour
    )

    assert bot.sent_messages == []
    assert user.settings.execution_digest_last_sent_date is None


async def test_reconciler_discrepancy_reaches_the_digest_through_the_db(ctx) -> None:  # type: ignore[no-untyped-def]
    """Шаг 15.6: событие reconciler за окно — в «Аномалиях» сводки."""
    from app.database.models.reconciliation_event import ReconciliationEvent
    from app.trading.enums import ReconciliationKind

    daily, session, user, bot, settings = ctx
    session.add(ReconciliationEvent(
        user_id=user.id, symbol="LINK-USDT", kind=ReconciliationKind.ORPHAN_POSITION,
        dedup_key="orphan:LINK-USDT:LONG:1", detail="позиция без сделки",
    ))
    await session.flush()

    now, _tz_offset, today_local, local_hour = _call_args(
        user, settings, local_hour=settings.exec_daily_digest_hour
    )
    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today_local, local_hour
    )

    assert "сверка с биржей: расхождений 1 (позиция без сделки — 1)" in bot.sent_messages[0][1]


async def test_digest_has_reconciler_pulse_line(ctx) -> None:  # type: ignore[no-untyped-def]
    """28.09: строка «Сверка:» — пульс reconciler в сводке исполнения."""
    from app.workers.reconciler import ReconcilerPulse

    daily, session, user, bot, settings = ctx

    class _Reconciler:
        pulse = ReconcilerPulse(datetime.now(UTC) - timedelta(days=2))

    _Reconciler.pulse.record_cycle(datetime.now(UTC) - timedelta(minutes=1), errors=0)
    daily._reconciler = _Reconciler()
    now, _tz_offset, today_local, local_hour = _call_args(
        user, settings, local_hour=settings.exec_daily_digest_hour
    )

    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today_local, local_hour
    )

    [(_chat, text)] = bot.sent_messages
    assert "Сверка: циклов 1, последний " in text
    assert text.rstrip().endswith("ошибок 0")
