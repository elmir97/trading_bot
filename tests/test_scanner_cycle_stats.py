"""Замер цикла SetupScanner.run() — раздел "троттлинг сканера".

Сеть не участвует: AnalysisEngine.build_context подменяется фейком, который
сам двигает scanner._client.request_count — так же, как двигал бы его
настоящий BingXClient._request() на каждый реальный вызов. Тест проверяет
только арифметику (символы/запросы/время считаются верно), не реальные
сетевые числа — это дело test_bingx_client.py.

UserRepository.list_active_with_plan подменяется на список из одного
тестового пользователя: база trading_bot_test за время жизни репозитория
накопила сотни активных пользователей от других интеграционных тестов
(они не подчищают за собой, см. test_signals_integration.py) — без этой
подмены run() честно сканирует их всех, и тест одновременно медленный и
проверяет не то число (сумму по всем пользователям, а не по одному своему).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.workers.scanner import SCAN_TIMEFRAMES, SetupScanner

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)


class FakeBot:
    async def send_message(self, chat_id, text, reply_markup=None):  # type: ignore[no-untyped-def]
        pass

    async def send_photo(self, chat_id, photo, caption, reply_markup=None):  # type: ignore[no-untyped-def]
        pass


@pytest_asyncio.fixture
async def ctx(monkeypatch):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        telegram_id = 810_000 + int(datetime.now(UTC).timestamp() * 1000) % 90_000
        user = await user_service.get_or_create(telegram_id=telegram_id)

        async def fake_list_active_with_plan(self):  # type: ignore[no-untyped-def]
            return [user]

        monkeypatch.setattr(
            UserRepository, "list_active_with_plan", fake_list_active_with_plan
        )

        bot = FakeBot()
        scanner = SetupScanner(bot, db, settings)
        yield user, scanner, settings
    await db.dispose()


async def test_cycle_counts_symbols_and_requests(ctx) -> None:  # type: ignore[no-untyped-def]
    user, scanner, _ = ctx
    requests_per_call = 3  # произвольное число — важна только арифметика diff'а

    async def fake_build_context(symbol, timeframe):  # type: ignore[no-untyped-def]
        scanner._client.request_count += requests_per_call
        return None  # ведёт в wait_signal(), сети не касается

    scanner._engine.build_context = fake_build_context  # type: ignore[assignment]

    await scanner.run()

    plan = user.trading_plan
    assert scanner.last_cycle is not None
    assert scanner.last_cycle.symbols_scanned == len(plan.allowed_symbols)
    # fake_build_context зовётся на каждый (символ, таймфрейм).
    expected_requests = len(plan.allowed_symbols) * len(SCAN_TIMEFRAMES) * requests_per_call
    assert scanner.last_cycle.requests_made == expected_requests
    assert scanner.last_cycle.duration_seconds >= 0


async def test_cycle_duration_reflects_elapsed_time(ctx) -> None:  # type: ignore[no-untyped-def]
    _, scanner, _ = ctx
    delay_seconds = 0.05

    async def fake_build_context(symbol, timeframe):  # type: ignore[no-untyped-def]
        await asyncio.sleep(delay_seconds)
        return None

    scanner._engine.build_context = fake_build_context  # type: ignore[assignment]

    await scanner.run()

    assert scanner.last_cycle is not None
    # Хотя бы один вызов заведомо длиннее одиночной задержки — не строгое
    # равенство (реальное время всегда чуть больше), а нижняя граница.
    assert scanner.last_cycle.duration_seconds >= delay_seconds


async def test_second_run_measures_only_its_own_delta(ctx) -> None:  # type: ignore[no-untyped-def]
    """requests_made — diff за ЭТОТ прогон, не растущий с процессом счётчик
    клиента (клиент общий и переживает между вызовами run(), см.
    комментарий у SetupScanner.__init__/self._client)."""
    _, scanner, _ = ctx

    async def fake_build_context(symbol, timeframe):  # type: ignore[no-untyped-def]
        scanner._client.request_count += 1
        return None

    scanner._engine.build_context = fake_build_context  # type: ignore[assignment]

    await scanner.run()
    first = scanner.last_cycle
    await scanner.run()
    second = scanner.last_cycle

    assert first is not None
    assert second is not None
    # Одинаковый план сканируется одинаково на обоих прогонах — второй
    # прогон не должен унаследовать запросы первого.
    assert second.requests_made == first.requests_made
    assert scanner._client.request_count == first.requests_made + second.requests_made
