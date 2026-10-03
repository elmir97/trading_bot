"""Доставка рассылок daily_jobs (28.09): дата отправки — только после
окончательного исхода.

Сбой сети (TelegramNetworkError) не ставит дату — следующий цикл повторит;
бот заблокирован — исход окончательный, один WARNING, без повторов; не
ушедшее до местной полуночи — выбрасывается с WARNING. Живой случай — 23.09
15:13 UTC: дневная сводка потеряна, дата уже стояла до отправки.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError

from app.core.config import Settings
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import PeriodPnl, TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode
from app.trading.risk import tz_offset_for
from app.workers.daily import DailyJobs
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")


class OkBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent.append(text)


class NetworkFailBot(OkBot):
    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        raise TelegramNetworkError(method=None, message="Request timeout error")  # type: ignore[arg-type]


class ForbiddenBot(OkBot):
    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent.append(text)
        raise TelegramForbiddenError(method=None, message="forbidden")  # type: ignore[arg-type]


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
        daily = DailyJobs(OkBot(), db, settings)  # type: ignore[arg-type]
        yield daily, session, user, settings
        await cleanup_user(session, user)
    await db.dispose()


def _today(user):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    tz_offset = tz_offset_for(user.settings.timezone)
    return now, tz_offset, (now + timedelta(hours=tz_offset)).date()


async def _digest(daily, session, user, settings):  # type: ignore[no-untyped-def]
    now, _tz, today = _today(user)
    await daily._maybe_send_execution_digest(
        session, user, user.settings, now, today, settings.exec_daily_digest_hour
    )


async def _summary(daily, session, user, settings):  # type: ignore[no-untyped-def]
    now, tz, today = _today(user)
    await daily._maybe_send_summary(
        session, user, user.settings, now, tz, today, settings.daily_summary_hour_local
    )


async def test_execution_digest_network_failure_keeps_date_and_retries(ctx) -> None:  # type: ignore[no-untyped-def]
    daily, session, user, settings = ctx
    daily._bot = NetworkFailBot()
    await _digest(daily, session, user, settings)
    assert user.settings.execution_digest_last_sent_date is None

    ok = OkBot()
    daily._bot = ok
    await _digest(daily, session, user, settings)
    await _digest(daily, session, user, settings)
    assert len(ok.sent) == 1
    assert user.settings.execution_digest_last_sent_date == _today(user)[2]


async def test_daily_summary_network_failure_keeps_date_and_retries(ctx) -> None:  # type: ignore[no-untyped-def]
    daily, session, user, settings = ctx
    daily._bot = NetworkFailBot()
    await _summary(daily, session, user, settings)
    assert user.settings.daily_summary_last_sent_date is None

    ok = OkBot()
    daily._bot = ok
    await _summary(daily, session, user, settings)
    assert len(ok.sent) == 1
    assert user.settings.daily_summary_last_sent_date == _today(user)[2]


async def test_loss_alert_network_failure_keeps_date_and_retries(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daily, session, user, _settings = ctx

    async def day_pnl(self, user_id, start, end, *, account_mode):  # type: ignore[no-untyped-def]
        return PeriodPnl(percent=Decimal("-10"), counted=1, uncounted=0)  # выше лимита 6%

    monkeypatch.setattr(TradeRepository, "pnl_percent_between", day_pnl)
    now, tz, today = _today(user)

    daily._bot = NetworkFailBot()
    await daily._maybe_send_loss_alert(session, user, user.settings, now, tz, today)
    assert user.settings.daily_loss_alert_last_sent_date is None

    ok = OkBot()
    daily._bot = ok
    await daily._maybe_send_loss_alert(session, user, user.settings, now, tz, today)
    assert len(ok.sent) == 1
    assert user.settings.daily_loss_alert_last_sent_date == today


async def test_forbidden_is_final_single_warning(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    daily, session, user, settings = ctx
    bot = ForbiddenBot()
    daily._bot = bot
    with caplog.at_level(logging.INFO):
        await _digest(daily, session, user, settings)
        await _digest(daily, session, user, settings)

    assert len(bot.sent) == 1  # второй цикл не повторяет
    assert user.settings.execution_digest_last_sent_date == _today(user)[2]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "бот заблокирован" in warnings[0].getMessage()


async def test_undelivered_is_dropped_after_local_midnight(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    daily, session, user, settings = ctx
    daily._bot = NetworkFailBot()
    await _digest(daily, session, user, settings)
    today = _today(user)[2]

    with caplog.at_level(logging.WARNING, logger="app.workers.daily"):
        daily._drop_stale_undelivered(user.id, today)  # те же сутки — не выбрасываем
        assert not [r for r in caplog.records if "выброшена" in r.getMessage()]
        daily._drop_stale_undelivered(user.id, today + timedelta(days=1))

    dropped = [r for r in caplog.records if "выброшена" in r.getMessage()]
    assert len(dropped) == 1
    assert "execution_digest" in dropped[0].getMessage()


async def test_successful_send_logs_info(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    daily, session, user, settings = ctx
    with caplog.at_level(logging.INFO, logger="app.workers.daily"):
        await _digest(daily, session, user, settings)

    assert [r for r in caplog.records if r.getMessage() == "Рассылка отправлена: execution_digest"]


async def test_loss_alert_same_account_percent_of_entry_balance(ctx) -> None:  # type: ignore[no-untyped-def]
    """03.10.2026: дневной алерт — процент от баланса на входе каждой сделки,
    отдельно по счёту из настроек и по ручным записям (вариант «а», блокер
    live Б3), каждая группа против своего лимита; другой счёт не учитывается;
    без баланса — «Не учтены: N»."""
    from tests.test_risk_limits_account import _closed

    daily, session, user, _settings = ctx
    now, tz, today = _today(user)
    mode = user.settings.active_exchange_mode
    other = ExchangeKeyMode.LIVE if mode is ExchangeKeyMode.DEMO else ExchangeKeyMode.DEMO
    await _closed(session, user, "-700", "10000", mode, closed_at=now)      # −7%
    await _closed(session, user, "-1.30", None, mode, closed_at=now)        # без баланса
    await _closed(session, user, "-5000", "1000", None, closed_at=now)      # ручная
    await _closed(session, user, "-5000", "1000", other, closed_at=now)     # другой счёт

    ok = OkBot()
    daily._bot = ok
    await daily._maybe_send_loss_alert(session, user, user.settings, now, tz, today)
    [text] = ok.sent
    assert (
        f"Счёт {mode.label}: −7.00% при лимите 6.00%. "
        "Не учтены: 1 (сделки без баланса на входе)."
    ) in text
    assert "Ручные записи (без счёта): −500.00% при лимите 6.00%." in text
    assert text.count("при лимите") == 2                   # другой счёт не учтён


async def test_loss_alert_manual_trades_alone(ctx) -> None:  # type: ignore[no-untyped-def]
    """Ручные записи без счёта достигли лимита, счёт из настроек — нет:
    алерт уходит одной строкой по ручным."""
    from tests.test_risk_limits_account import _closed

    daily, session, user, _settings = ctx
    now, tz, today = _today(user)
    await _closed(session, user, "-70", "1000", None, closed_at=now)                 # −7%
    await _closed(session, user, "-10", "10000", user.settings.active_exchange_mode,
                  closed_at=now)                                                     # −0.1%
    ok = OkBot()
    daily._bot = ok
    await daily._maybe_send_loss_alert(session, user, user.settings, now, tz, today)
    [text] = ok.sent
    assert "Ручные записи (без счёта): −7.00% при лимите 6.00%." in text
    assert "Счёт" not in text
