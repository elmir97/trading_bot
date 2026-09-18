"""Тесты DailyJobs._maybe_send_execution_digest (этап 15.4, раздел 12а ТЗ).

Против настоящей БД (нужны User/UserSettings/TradingPlan/ExecutionOrder —
как остальные интеграционные тесты этапа 15), Bot — свой минимальный дублёр
(как FakeBot в tests/test_notifier.py), в Telegram ничего не уходит.
Проверяется именно склейка DailyJobs с execution_digest.build_stats/
render_execution_digest и три её собственных условия отправки (переключатель,
"уже отправляли сегодня", час) — подсчёты и аномалии уже покрыты
tests/test_execution_digest.py.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.core.security import SecretCipher
from app.database.models.execution_order import ExecutionOrder
from app.database.models.signal import SignalRecord
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import (
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalLevel,
    TradeSide,
)
from app.trading.risk import tz_offset_for
from app.workers.daily import DailyJobs
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal


class FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent_messages.append((chat_id, text))


def _row(user_id: int, status: OrderStatus, **overrides: object) -> ExecutionOrder:
    fields: dict[str, object] = {
        "user_id": user_id,
        "symbol": "BTC-USDT",
        "side": OrderSide.BUY,
        "position_side": TradeSide.LONG,
        "order_type": OrderType.MARKET,
        "role": OrderRole.ENTRY,
        "status": status,
    }
    fields.update(overrides)
    return ExecutionOrder(**fields)  # type: ignore[arg-type]


def _signal(
    user_id: int, level: SignalLevel, *, symbol: str, notified_at: datetime, **overrides: object
) -> SignalRecord:
    fields: dict[str, object] = {
        "user_id": user_id,
        "symbol": symbol,
        "timeframe": "1h",
        "level": level,
        "setup": "test",
        "fingerprint": f"fp-{symbol}-{level}",
        "detail": "test detail",
        "expires_at": notified_at + timedelta(hours=1),
        "notified_at": notified_at,
    }
    fields.update(overrides)
    return SignalRecord(**fields)  # type: ignore[arg-type]


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


async def test_sends_digest_reflecting_todays_rows(ctx) -> None:  # type: ignore[no-untyped-def]
    daily, session, user, bot, settings = ctx
    now = datetime.now(UTC)
    # Окно теперь window_start..now с исключающей верхней границей (как и
    # раньше у day_bounds) — строки должны лечь строго ДО now, иначе флюш
    # может сравняться с now до микросекунды и вылететь из окна. В проде
    # так и есть: данные всегда написаны раньше, чем DailyJobs захватит now.
    moment = now - timedelta(minutes=1)
    session.add(
        _row(
            user.id, OrderStatus.DRY_RUN, risk_percent=D("1.0"), risk_reward=D("2.0"),
            created_at=moment,
        )
    )
    session.add(_row(user.id, OrderStatus.DECLINED, created_at=moment))
    session.add(_row(user.id, OrderStatus.REFUSED, error_code="MAX_POSITIONS", created_at=moment))
    # READY-сигнал сегодня — должен попасть в счётчик. FORMING сегодня же —
    # проверяет, что фильтр по level реально отсекает не-READY.
    session.add(_signal(user.id, SignalLevel.READY, symbol="BTC-USDT", notified_at=moment))
    session.add(_signal(user.id, SignalLevel.FORMING, symbol="ETH-USDT", notified_at=moment))
    await session.flush()

    _now, _tz_offset, today_local, local_hour = _call_args(
        user, settings, local_hour=settings.exec_daily_digest_hour
    )
    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today_local, local_hour
    )

    assert len(bot.sent_messages) == 1
    _chat_id, text = bot.sent_messages[0]
    assert "Сигналов READY: 1" in text
    assert "показана карточка: 2" in text
    assert "подтверждено: 1" in text
    assert "отказ пользователя: 1" in text
    assert "MAX_POSITIONS — 1" in text
    assert user.settings.execution_digest_last_sent_date == today_local


async def test_window_is_rolling_24h_not_calendar_day(ctx) -> None:  # type: ignore[no-untyped-def]
    """Регрессия на баг из разведки: при часе отправки, отличном от локальной
    полночи, календарные сутки (day_bounds) резали бы события между часом
    отправки и полночью — они не попадали бы ни в сегодняшнюю сводку (её уже
    нет), ни в завтрашнюю (окно уже следующего дня). Окно теперь строго
    "последние 24 часа до now", без оглядки на календарную границу: строка
    23 часа назад видна, строка 25 часов назад — нет, независимо от того, где
    по местному времени проходит полночь."""
    daily, session, user, bot, settings = ctx
    now = datetime.now(UTC)

    session.add(
        _row(
            user.id, OrderStatus.DECLINED, created_at=now - timedelta(hours=23)
        )
    )
    session.add(
        _row(
            user.id, OrderStatus.EXPIRED, created_at=now - timedelta(hours=25)
        )
    )
    session.add(
        _signal(
            user.id, SignalLevel.READY, symbol="BTC-USDT", notified_at=now - timedelta(hours=23)
        )
    )
    session.add(
        _signal(
            user.id, SignalLevel.READY, symbol="ETH-USDT", notified_at=now - timedelta(hours=25)
        )
    )
    await session.flush()

    _now, _tz_offset, today_local, local_hour = _call_args(
        user, settings, local_hour=settings.exec_daily_digest_hour
    )
    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today_local, local_hour
    )

    assert len(bot.sent_messages) == 1
    text = bot.sent_messages[0][1]
    # 23ч назад — внутри окна: 1 READY-сигнал, 1 показанная карточка (отказ
    # пользователя). 25ч назад — вне окна, не должно попасть ни в одно число.
    assert "Сигналов READY: 1" in text
    assert "показана карточка: 1" in text
    assert "отказ пользователя: 1" in text
    assert "истекло по TTL: 0" in text


async def test_zero_signals_day_still_sends_digest(ctx) -> None:  # type: ignore[no-untyped-def]
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
    assert "Сигналов READY: 0" in text
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
