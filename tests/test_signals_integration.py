"""Интеграционный тест дедупа сканера сетапов — против настоящей БД.

Проверяет буквально требование 3: "один сетап — одно уведомление, повтор
только если условия изменились или прошло N часов." SetupScanner._handle_signal
дергается напрямую с готовым Signal — сеть (BingX) при этом не участвует,
только персистентность и решение "слать или нет".
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.analysis.signals import MarketContext, Signal, SignalCondition
from app.core.config import Settings
from app.database.repositories.signal import SignalRepository
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ExchangeUnavailableError, Kline, SymbolInfo
from app.services.user_service import UserService
from app.trading.enums import MarketStructure, SignalDirection, SignalLevel, SignalRecordStatus
from app.workers.scanner import SetupScanner
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)

D = __import__("decimal").Decimal


class FakeBot:
    """Не настоящий aiogram.Bot: send_notification(_photo) зовёт только
    send_message/send_photo.

    reply_markup принимается и запоминается отдельно: READY-уведомления
    (этап 15.4) приходят с кнопкой "Открыть сделку", FORMING — без неё
    (см. app/workers/scanner.py и test_ready_notification_has_open_button)."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.sent_photos: list[tuple[int, str]] = []
        self.sent_markups: list[object] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent.append((chat_id, text))
        self.sent_markups.append(reply_markup)

    async def send_photo(self, chat_id: int, photo, caption: str, reply_markup=None) -> None:  # type: ignore[no-untyped-def]
        self.sent_photos.append((chat_id, caption))
        self.sent_markups.append(reply_markup)


async def _fake_symbol_info(symbol: str) -> SymbolInfo:
    return SymbolInfo(symbol, 2, 4, D("0.0001"), 125)


def _context() -> MarketContext:
    now = datetime.now(UTC)
    price = D("100")
    candles = []
    for i in range(220):
        price += D(i % 5 - 2) * D("0.3")
        open_time = now - timedelta(hours=220 - i)
        candles.append(
            Kline(
                open_time=open_time, open=price, high=price + D("1"), low=price - D("1"),
                close=price + D("0.2"), volume=D("1000"), close_time=open_time + timedelta(hours=1),
            )
        )
    return MarketContext(
        symbol="BTC-USDT", timeframe="4h", candles=candles, price=candles[-1].close,
        ema20=D("99"), ema50=D("98"), ema200=D("95"), rsi=D("55"), atr=D("1.5"),
        macd_histogram=D("0.1"), volume_ratio=D("1.2"), structure=MarketStructure.UPTREND,
        levels=[],
    )


def _ready_signal(entry_low: str = "100") -> Signal:
    return Signal(
        symbol="BTC-USDT",
        timeframe="4h",
        direction=SignalDirection.LONG,
        setup="Пробой с ретестом",
        entry_zone_low=D(entry_low),
        entry_zone_high=D(entry_low) + D("1"),
        stop_loss=D("98"),
        take_profit_1=D("106"),
        risk_reward=D("2.5"),
        confidence=7,
        conditions=[SignalCondition("Фильтр EMA200", True, "")],
    )


def _forming_signal() -> Signal:
    return Signal(
        symbol="BTC-USDT",
        timeframe="4h",
        direction=SignalDirection.WAIT,
        setup="Нет сетапа",  # так во всех WAIT-сигналах в реальном коде
        note="Цена на ретесте, но подтверждения нет.",
        conditions=[
            SignalCondition("Фильтр EMA200", True, ""),
            SignalCondition("Подтверждающий паттерн", False, ""),
        ],
    )


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        user = await user_service.get_or_create(telegram_id=unique_telegram_id())
        bot = FakeBot()
        scanner = SetupScanner(bot, db, settings)
        # Точность символа сканер берёт у биржи (get_symbol_info) перед рендером
        # графика; в тестах сеть не участвует — подменяем на фейк.
        scanner._engine.get_symbol_info = _fake_symbol_info  # type: ignore[method-assign]
        repo = SignalRepository(session)
        yield user, session, repo, scanner, bot, settings
        await cleanup_user(session, user)
    await db.dispose()


async def test_new_setup_notifies_once(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo, scanner, bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _ready_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()

    assert len(bot.sent) == 1
    record = await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.READY)
    assert record is not None
    assert record.status == SignalRecordStatus.ACTIVE


async def test_same_setup_within_ttl_does_not_repeat(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo, scanner, bot, _ = ctx

    for _ in range(3):
        await scanner._handle_signal(
            repo, user, _ready_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
        )
    await session.flush()

    assert len(bot.sent) == 1  # тот же сетап трижды подряд — уведомление одно


async def test_changed_price_notifies_again(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo, scanner, bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _ready_signal("100"), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await scanner._handle_signal(
        repo, user, _ready_signal("110"), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()

    assert len(bot.sent) == 2  # вход заметно сместился — условия "изменились"


async def test_expired_ttl_notifies_again(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo, scanner, bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _ready_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()

    record = await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.READY)
    assert record is not None
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)  # искусственно истекло
    await session.flush()

    await scanner._handle_signal(
        repo, user, _ready_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()

    assert len(bot.sent) == 2  # прошло "N часов" — повтор, хотя сетап тот же


async def test_setup_disappearing_expires_both_slots(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo, scanner, _bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()
    forming = await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.FORMING)
    assert forming is not None

    neither = Signal(
        symbol="BTC-USDT", timeframe="4h", direction=SignalDirection.WAIT,
        setup="Пробой с ретестом",
        conditions=[SignalCondition("Пробой уровня", False, "")],
    )
    await scanner._handle_signal(
        repo, user, neither, "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()

    forming_after = await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.FORMING)
    assert forming_after is None  # слот погашен, не дожидаясь TTL


async def test_setup_reappearing_after_expiry_reuses_slot(ctx) -> None:  # type: ignore[no-untyped-def]
    """uq_signal_slot не включает status: EXPIRED-строка всё ещё занимает
    (user, symbol, timeframe, level), поэтому сетап, вернувшийся после
    погашения слота, обязан переиспользовать (UPDATE) старую строку, а
    не падать на INSERT с IntegrityError — и обязан уведомить заново."""
    user, session, repo, scanner, bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()

    gone = Signal(
        symbol="BTC-USDT", timeframe="4h", direction=SignalDirection.WAIT,
        setup="Пробой с ретестом",
        conditions=[SignalCondition("Пробой уровня", False, "")],
    )
    await scanner._handle_signal(
        repo, user, gone, "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()
    assert await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.FORMING) is None

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()  # раньше здесь падал IntegrityError на uq_signal_slot

    record = await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.FORMING)
    assert record is not None
    assert record.status == SignalRecordStatus.ACTIVE
    assert len(bot.sent) == 2  # погасло, затем появилось заново — второе уведомление


async def test_forming_promoted_to_ready_expires_forming_slot(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo, scanner, bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await scanner._handle_signal(
        repo, user, _ready_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()

    forming = await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.FORMING)
    ready = await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.READY)
    assert forming is None
    assert ready is not None
    assert len(bot.sent) == 2  # уведомление и про forming, и про ready


async def test_disabled_level_is_never_persisted(ctx) -> None:  # type: ignore[no-untyped-def]
    user, session, repo, scanner, bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=False
    )
    await session.flush()

    assert bot.sent == []
    assert await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.FORMING) is None


async def test_chart_sent_as_photo_when_setting_enabled(ctx) -> None:  # type: ignore[no-untyped-def]
    """setup_charts включён по умолчанию (DEFAULT_NOTIFICATIONS) — при
    наличии MarketContext уведомление уходит фотографией, не текстом."""
    user, session, repo, scanner, bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h",
        want_ready=True, want_forming=True, context=_context(),
    )
    await session.flush()

    assert len(bot.sent_photos) == 1
    assert bot.sent == []  # текстом отдельно не дублируется


async def test_chart_skipped_when_setting_disabled(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    user, session, repo, scanner, bot, _ = ctx
    user.settings.notifications = {**user.settings.notifications, "setup_charts": False}
    await session.flush()

    calls: list[object] = []
    monkeypatch.setattr(
        "app.workers.scanner.render_setup_chart",
        lambda *a, **kw: calls.append(1) or b"unused",
    )

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h",
        want_ready=True, want_forming=True, context=_context(),
    )
    await session.flush()

    assert calls == []  # рендер не должен даже запускаться
    assert bot.sent_photos == []
    assert len(bot.sent) == 1  # текст ушёл как обычно


async def test_chart_render_failure_still_sends_text(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Требование: если график не построился, текст всё равно уходит —
    ровно одно уведомление, не ноль."""
    user, session, repo, scanner, bot, _ = ctx
    monkeypatch.setattr("app.workers.scanner.render_setup_chart", lambda *a, **kw: None)

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h",
        want_ready=True, want_forming=True, context=_context(),
    )
    await session.flush()

    assert bot.sent_photos == []
    assert len(bot.sent) == 1


async def test_ready_notification_has_open_trade_button(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 5 ТЗ этапа 15: кнопка "Открыть сделку" — только под READY."""
    user, session, repo, scanner, bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _ready_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()

    assert len(bot.sent_markups) == 1
    markup = bot.sent_markups[0]
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.text == "⚡ Открыть сделку" for b in buttons)

    record = await repo.get_active_slot(user.id, "BTC-USDT", "4h", SignalLevel.READY)
    assert record is not None
    assert any(b.callback_data == f"exec:open:{record.id}" for b in buttons)


async def test_forming_notification_has_no_open_trade_button(ctx) -> None:  # type: ignore[no-untyped-def]
    """Под FORMING кнопка входа не появляется никогда (раздел 5 ТЗ)."""
    user, session, repo, scanner, bot, _ = ctx

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()

    assert len(bot.sent_markups) == 1
    assert bot.sent_markups[0] is None


async def test_manual_analysis_writes_nothing_and_does_not_steal_scanner_notification(  # type: ignore[no-untyped-def]
    ctx, monkeypatch
) -> None:
    """Разовый анализ по кнопке не пишет в signals: иначе строка от кнопки
    заняла бы слот и сканер не прислал бы первое уведомление (existing с тем
    же fingerprint → should_notify=False), а READY-строка стала бы
    исполнимой в обход TTL и дедупа."""
    from sqlalchemy import func, select

    from app.bot.handlers import analysis as screen
    from app.database.models.signal import SignalRecord
    from tests.test_analysis_screen import _callback, _Client, _Engine, _message

    user, session, repo, scanner, bot, _ = ctx

    async def count() -> int:
        result = await session.execute(
            select(func.count()).select_from(SignalRecord).where(SignalRecord.user_id == user.id)
        )
        return int(result.scalar_one())

    async def fake_engine(_settings):  # type: ignore[no-untyped-def]
        return _Engine(_ready_signal()), _Client()

    monkeypatch.setattr(screen, "_engine", fake_engine)
    screen._in_flight.clear()

    assert await count() == 0
    await screen.show_market(_callback(_message()), user, settings=None)  # type: ignore[arg-type]
    await session.flush()
    assert await count() == 0  # кнопка «Анализ рынка» ничего не записала

    await scanner._handle_signal(
        repo, user, _ready_signal(), "BTC-USDT", "4h", want_ready=True, want_forming=True
    )
    await session.flush()
    assert len(bot.sent) == 1  # сканер уведомил как в первый раз


async def test_chart_gets_price_precision_from_exchange(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    user, _, repo, scanner, _, _ = ctx
    seen: list[tuple] = []
    monkeypatch.setattr(
        "app.workers.scanner.render_setup_chart", lambda *a: seen.append(a) or b"png"
    )

    async def info(symbol: str) -> SymbolInfo:
        return SymbolInfo(symbol, 3, 4, D("0.0001"), 125)

    scanner._engine.get_symbol_info = info  # type: ignore[method-assign]

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h",
        want_ready=True, want_forming=True, context=_context(),
    )

    assert len(seen) == 1
    assert seen[0][3] == 3  # price_precision дошёл до render_setup_chart


async def test_symbol_info_failure_falls_back_and_chart_is_still_sent(  # type: ignore[no-untyped-def]
    ctx, caplog
) -> None:
    """Биржа недоступна при запросе списка инструментов: график всё равно
    строится (реальный рендер), подписи откатываются на fallback, в логе
    предупреждение — не молчаливая подмена."""
    user, session, repo, scanner, bot, _ = ctx

    async def down(symbol: str) -> SymbolInfo:
        raise ExchangeUnavailableError("down")

    scanner._engine.get_symbol_info = down  # type: ignore[method-assign]

    with caplog.at_level("WARNING"):
        await scanner._handle_signal(
            repo, user, _forming_signal(), "BTC-USDT", "4h",
            want_ready=True, want_forming=True, context=_context(),
        )
    await session.flush()

    assert len(bot.sent_photos) == 1  # график ушёл, уведомление не потеряно
    assert bot.sent == []
    assert "Не удалось получить точность цены" in caplog.text


async def test_symbol_info_failure_passes_none_precision(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    user, _, repo, scanner, _, _ = ctx
    seen: list[tuple] = []
    monkeypatch.setattr(
        "app.workers.scanner.render_setup_chart", lambda *a: seen.append(a) or b"png"
    )

    async def down(symbol: str) -> SymbolInfo:
        raise RuntimeError("любой сбой")

    scanner._engine.get_symbol_info = down  # type: ignore[method-assign]

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h",
        want_ready=True, want_forming=True, context=_context(),
    )

    assert seen[0][3] is None


async def test_unknown_symbol_gives_none_precision(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Символа нет в списке биржи (get_symbol_info вернул None) — fallback."""
    user, _, repo, scanner, _, _ = ctx
    seen: list[tuple] = []
    monkeypatch.setattr(
        "app.workers.scanner.render_setup_chart", lambda *a: seen.append(a) or b"png"
    )

    async def missing(symbol: str) -> None:
        return None

    scanner._engine.get_symbol_info = missing  # type: ignore[method-assign]

    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h",
        want_ready=True, want_forming=True, context=_context(),
    )

    assert seen[0][3] is None


async def test_symbol_info_is_not_requested_without_a_chart(ctx) -> None:  # type: ignore[no-untyped-def]
    """Запрос только когда график реально рисуется: не при выключенных
    графиках и не при повторе того же сетапа (дедуп — уведомления нет)."""
    user, session, repo, scanner, _, _ = ctx
    calls: list[str] = []

    async def counting(symbol: str) -> SymbolInfo:
        calls.append(symbol)
        return SymbolInfo(symbol, 2, 4, D("0.0001"), 125)

    scanner._engine.get_symbol_info = counting  # type: ignore[method-assign]

    # 1) первое уведомление с графиком — один запрос
    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h",
        want_ready=True, want_forming=True, context=_context(),
    )
    await session.flush()
    assert calls == ["BTC-USDT"]

    # 2) тот же сетап в пределах TTL — уведомления нет, запроса нет
    await scanner._handle_signal(
        repo, user, _forming_signal(), "BTC-USDT", "4h",
        want_ready=True, want_forming=True, context=_context(),
    )
    assert calls == ["BTC-USDT"]

    # 3) графики выключены — запроса нет
    user.settings.notifications = {**user.settings.notifications, "setup_charts": False}
    await session.flush()
    await scanner._handle_signal(
        repo, user, _ready_signal(), "ETH-USDT", "4h",
        want_ready=True, want_forming=True, context=_context(),
    )
    assert calls == ["BTC-USDT"]
