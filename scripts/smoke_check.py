"""Полная проверка бота перед выкаткой.

Проходит все разделы меню и все команды, отмечая проблемы. Задача —
поймать то, что не видят юнит-тесты: пустые ответы, необработанные
кнопки, застрявшие состояния, ошибки в связках между разделами.

Запуск: python -m scripts.smoke_check
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, ".")

from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.locks import RedisLock, confirm_lock_key
from app.core.security import SecretCipher, mask_secret
from app.database.models.credentials import ExchangeCredentials
from app.database.models.execution_order import ExecutionOrder
from app.database.models.signal import SignalRecord
from app.database.models.signal_notification import SignalNotification
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import Balance, Position, SymbolInfo, Ticker
from app.exchanges.bingx import BingXClient
from app.trading.enums import ExchangeKeyMode, SignalDirection, SignalLevel, SignalRecordStatus
from scripts.simulate_chat import USER_ID, build  # noqa: E402

# Скрипт пишет и удаляет реальные строки — только тестовая база. Имя, а не
# просто "не совпадает с проду по случайности": DATABASE_URL может указывать
# на что угодно, включая прод по опечатке в окружении запуска.
REQUIRED_DB_NAME = "trading_bot_test"

problems: list[str] = []
checks = {"total": 0, "passed": 0}


def _ensure_test_database(database_url: str) -> None:
    name = make_url(database_url).database
    if name != REQUIRED_DB_NAME:
        print(
            f"Отказ: DATABASE_URL указывает на базу «{name}», а не на "
            f"«{REQUIRED_DB_NAME}». smoke_check.py пишет и удаляет реальные "
            "строки — запускать его можно только против тестовой базы."
        )
        sys.exit(1)


def _ensure_execution_enabled(execution_enabled: bool) -> None:
    """Без TRADING_EXECUTION_ENABLED=true guard EXECUTION_DISABLED в
    evaluate() отказывает раньше похода за ценой — раздел [14] не строит
    карточку и падает шестью разными проверками с непохожими друг на
    друга деталями (одна REFUSED-строка вместо трёх DRY_RUN и т.п.),
    выглядя как баг execution-пути, а не как забытая переменная запуска."""
    if not execution_enabled:
        print(
            "Отказ: TRADING_EXECUTION_ENABLED не включён. Раздел [14] "
            "(execution-путь) не сможет построить карточку подтверждения "
            "и упадёт шестью проверками, которые выглядят как баг, а не как "
            "забытая переменная окружения. Запусти с "
            "TRADING_EXECUTION_ENABLED=true."
        )
        sys.exit(1)


async def _find_leftover_user(db: Database) -> bool:
    """Своя область данных — весь след скрипта висит на этом telegram_id
    (см. USER_ID/CHAT_ID в scripts/simulate_chat.py). Каскады FK убирают
    детей одним DELETE FROM users — проверено по каждой таблице, которую
    пишет скрипт (везде ForeignKey("users.id", ondelete="CASCADE") в
    app/database/models/*.py):
      user_settings, trading_plans, strategies, trades (→ trade_fills,
      trade_mistakes), signals, exchange_credentials.
    Исключение — mistake_types: общий справочник (user_id IS NULL у
    системных строк), скрипт его не создаёт, только читает по кодам."""
    async with db.session() as session:
        row = await session.scalar(select(User.id).where(User.telegram_id == USER_ID))
    return row is not None


async def _cleanup_smoke_data(db: Database) -> None:
    async with db.session() as session:
        await session.execute(delete(User).where(User.telegram_id == USER_ID))


def check(name: str, condition: bool, detail: str = "") -> None:
    checks["total"] += 1
    if condition:
        checks["passed"] += 1
        print(f"  ✓ {name}")
    else:
        problems.append(f"{name}: {detail}")
        print(f"  ✗ {name} — {detail}")


def has(text: str, *fragments: str) -> bool:
    return all(f.lower() in text.lower() for f in fragments)


async def add_trade(sim, symbol, side, entry, sl, tp, balance="10000"):  # type: ignore[no-untyped-def]
    await sim.send("/start")
    await sim.tap("Добавить сделку")
    await sim.tap(symbol)
    await sim.tap(side)
    await sim.send(entry)
    await sim.send(sl)
    await sim.send(tp)
    await sim.tap("Рассчитать от риска")
    await sim.send(balance)
    await sim.send("10")
    await sim.tap("ретест")
    await sim.tap("4H")
    return await sim.send("тестовый сетап")


async def close_trade(sim, symbol, exit_price, fee="10", mistakes=()):  # type: ignore[no-untyped-def]
    await sim.send("/start")
    await sim.tap("Открытые")
    await sim.tap(f"{symbol}-USDT")
    await sim.send(exit_price)
    await sim.send(fee)
    text = await sim.send("тест выхода")
    for mistake in mistakes:
        await sim.tap(mistake)
    if "ошибки" in text.lower():
        text = await sim.tap("Готово")
    return text


async def main() -> None:
    settings = get_settings()
    _ensure_test_database(settings.database_url.get_secret_value())
    _ensure_execution_enabled(settings.trading_execution_enabled)

    sim, tg, db, redis = await build()

    if await _find_leftover_user(db):
        print(
            f"Отказ: в базе уже есть данные smoke-теста (telegram_id={USER_ID}) — "
            "похоже, прошлый прогон упал до уборки в finally. Разберись, почему "
            f"(смотри лог того прогона), и убери руками перед новым запуском:\n"
            f"  DELETE FROM users WHERE telegram_id = {USER_ID};  -- каскад доберёт детей"
        )
        await redis.aclose()
        await db.dispose()
        sys.exit(1)

    try:
        await _run_scenarios(sim, tg, db, redis, settings)
    finally:
        # Выполняется и при падении сценария в середине — без этого база
        # копит мусор от каждого прогона (см. историю: 860 строк в trades).
        await _cleanup_smoke_data(db)
        await redis.aclose()
        await db.dispose()

    print("\n" + "=" * 60)
    print(f"Проверок: {checks['total']}, успешно: {checks['passed']}")
    if problems:
        print(f"\nПРОБЛЕМЫ ({len(problems)}):")
        for p in problems:
            print(f"  • {p}")
        # check() только копит `problems` и печатает "✗" — без этого exit
        # code всегда 0, и упавшая проверка (например, execution_orders не
        # DRY_RUN) тонет в выводе, а не останавливает деплой/CI.
        sys.exit(1)
    else:
        print("Проблем не найдено.")


async def _run_scenarios(sim, tg, db, redis, settings) -> None:  # type: ignore[no-untyped-def]
    print("\n[1] Базовые команды")
    text = await sim.send("/start")
    check("/start открывает меню", has(text, "торговый журнал", "выбери раздел"), text[:60])
    check("меню содержит все разделы", len(sim.available_buttons()) == 12,
          f"кнопок {len(sim.available_buttons())}")

    text = await sim.send("/ping")
    check("/ping отвечает", "мс" in text, text[:60])

    text = await sim.send("/help")
    check("/help перечисляет команды", has(text, "/stats", "/trade"), text[:60])

    text = await sim.send("/plan")
    check("/plan показывает план", has(text, "риск на сделку: 2%"), text[:80])
    check("/plan без хвоста нулей", "2.0000" not in text, "формат Decimal")

    print("\n[2] Пустое состояние")
    for label, fragment in [
        ("Статистика", "сделок"), ("Открытые", "нет"),
        ("Сделки", "нет"), ("Просадка", "нет"),
    ]:
        await sim.send("/start")
        text = await sim.tap(label)
        check(f"«{label}» на пустой базе", len(text) > 10, "пустой ответ")

    # «Ошибки» убрали из меню (дублировала «Анализ ошибок»), но команда
    # /mistakes с числовой статистикой по-прежнему работает.
    text = await sim.send("/mistakes")
    check("«/mistakes» на пустой базе", len(text) > 10, "пустой ответ")

    print("\n[3] Добавление сделки")
    text = await add_trade(sim, "BTC", "LONG", "100500", "99500", "102500")
    check("сделка сохраняется", has(text, "сделка записана"), text[:80])
    check("объём рассчитан", has(text, "объём: 0.2"), text[:200])
    check("риск верный", has(text, "риск: 2%"), text[:200])
    check("плановый RR", has(text, "1:2"), text[:200])

    print("\n[4] Закрытие и разметка ошибок")
    text = await close_trade(sim, "BTC", "102500", mistakes=("Ранний выход",))
    check("сделка закрывается", has(text, "сделка закрыта"), text[:80])
    check("PnL верный", "390" in text, text[:200])
    check("результат в R", "2R" in text, text[:200])
    check("ошибка записана", has(text, "ранний выход"), text[:250])

    print("\n[5] Статистика с данными")
    await sim.send("/start")
    text = await sim.tap("Статистика")
    check("win rate", has(text, "win rate: 100"), text[:150])
    check("PnL", "390" in text, text[:150])
    check("profit factor без убытков", "убыточных сделок нет" in text.lower(),
          text[:250])

    for label in ("По инструментам", "По стратегиям", "LONG / SHORT", "По времени"):
        text = await sim.tap(label)
        check(f"срез «{label}»", len(text) > 30, "пустой срез")

    print("\n[6] Убыточная сделка и метрики")
    await add_trade(sim, "ETH", "LONG", "3000", "2940", "3120")
    await close_trade(sim, "ETH", "2940", fee="5", mistakes=("FOMO",))

    await sim.send("/start")
    text = await sim.tap("Статистика")
    check("win rate пересчитан", has(text, "win rate: 50"), text[:150])
    check("profit factor появился", "profit factor:" in text.lower()
          and "нет" not in text.split("Profit Factor:")[1][:20].lower(), text[:250])
    check("просадка учтена", "просадка" in text.lower(), text[:250])

    text = await sim.send("/mistakes")
    check("анализ ошибок с деньгами", has(text, "fomo", "usdt"), text[:200])
    check("сравнение со средней", "средн" in text.lower(), text[:250])

    print("\n[7] Калькулятор риска")
    await sim.send("/start")
    await sim.tap("🧮 Риск")
    await sim.send("10000")
    await sim.tap("По плану")
    await sim.send("100.50")
    text = await sim.send("98.50")
    check("объём по методичке", has(text, "объём: 100"), text[:200])
    check("маржа по плечам", has(text, "10x"), text[:250])
    check("сумма риска без плюса", "+200" not in text, "ложный знак прибыли")

    text = await sim.send("104.50")
    check("RR рассчитан", has(text, "1:2"), text[:200])
    check("вердикт по методологии", "методолог" in text.lower(), text[:250])

    print("\n[8] Настройки")
    await sim.send("/start")
    text = await sim.tap("Настройки")
    check("настройки открываются", has(text, "риск на сделку"), text[:100])
    check("ключи не подключены", "не подключены" in text.lower(), text[:200])

    await sim.tap("Риск на сделку")
    text = await sim.send("3")
    check("риск меняется", has(text, "3%"), text[:100])
    check("предупреждение о риске", "потолка" in text.lower(), text[:200])

    await sim.send("/start")
    await sim.tap("Настройки")
    await sim.tap("Риск на сделку")
    await sim.send("2")

    print("\n[9] Биржа без ключей")
    await sim.send("/start")
    text = await sim.tap("Биржа")
    check("раздел биржи", has(text, "bingx"), text[:80])
    # Экран "Биржа" (app/bot/handlers/exchange_menu.py) показывает режим
    # счёта (ExchangeKeyMode.label — "🟢 Реальный" или "🧪 Демо") и статус
    # ключей — памятка про уровень доступа ("только на чтение") здесь не
    # выводится, она на экране /import (см. app/bot/handlers/exchange.py).
    check(
        "режим и статус ключей",
        (has(text, "реальный") or has(text, "демо")) and "не подключены" in text.lower(),
        text[:150],
    )

    text = await sim.send("/balance")
    check("баланс без ключей объясняет", "не подключены" in text.lower(), text[:120])

    text = await sim.send("/import")
    check(
        "импорт без ключей: требует ключ только на чтение",
        ("не подключены" in text.lower() or "требует ключей" in text.lower())
        and "только на чтение" in text.lower(),
        text[:250],
    )

    print("\n[10] Защита формы командой")
    await sim.send("/start")
    await sim.tap("Добавить сделку")
    await sim.tap("BTC")
    text = await sim.send("/stats")
    check("команда не попадает в форму", "направление" not in text.lower(), text[:120])
    check("форма отменяется с пояснением", "отменено" in text.lower(), text[:120])
    text = await sim.send("/stats")
    check("повторная команда работает", "win rate" in text.lower(), text[:120])

    print("\n[11] Некорректный ввод")
    await sim.send("/start")
    await sim.tap("Добавить сделку")
    await sim.tap("BTC")
    await sim.tap("LONG")
    text = await sim.send("не число")
    check("нечисловая цена отклонена", "не понял" in text.lower(), text[:100])
    await sim.send("100000")
    text = await sim.send("101000")
    check("стоп с неверной стороны отклонён", "ниже" in text.lower(), text[:120])
    await sim.send("/start")

    print("\n[12] Отчёты")
    await sim.send("/start")
    text = await sim.tap("Отчёт")
    check("сводный отчёт", has(text, "сегодня", "неделя"), text[:150])

    for command in ("/today", "/week", "/month"):
        text = await sim.send(command)
        check(f"{command}", len(text) > 20, "пустой отчёт")

    print("\n[13] Анализ рынка")
    # Раньше был заглушкой («этап 9»), теперь настоящий раздел — сеть не
    # дёргаем (данные с BingX живые), проверяем только сам экран выбора
    # инструмента, как и остальной смок-тест избегает внешних запросов.
    await sim.send("/start")
    text = await sim.tap("Анализ рынка")
    check("раздел не заглушка", "этап" not in text.lower(), text[:100])
    check("просит выбрать инструмент", has(text, "выбери инструмент"), text[:150])

    print("\n[14] Execution: карточка подтверждения")
    await _run_execution_scenario(sim, tg, db, redis, settings)


# --- [14] Execution: карточка подтверждения, сухой прогон, лок --------------
#
# ExecutionService.evaluate() (app/execution/service.py) трогает биржу в
# трёх местах: get_ticker (цена), get_symbols→get_symbol_info (лот/тик),
# get_balance. _build_quote() (app/bot/handlers/execution.py) добавляет
# четвёртое место сама, ДО evaluate(): get_position_mode() — раздел 16 ТЗ,
# шаг 15.5.1, гвард POSITION_MODE_UNKNOWN. Пятое — get_positions() карточки,
# шаг 15.5.4а, гвард EXCHANGE_POSITION_EXISTS. Подставляем все пять на уровне
# готовых типизированных методов BingXClient — то же, что делает
# FakeExchangeClient в tests/test_execution_service.py, но там клиент
# подставляется через конструктор ExecutionService, а здесь настоящий
# хендлер сам строит BingXClient внутри ExchangeFactory.for_user(), поэтому
# патчим пять leaf-методов на классе на время сценария и возвращаем
# оригиналы в finally. ExchangeFactory и MarketDataService не подменяются:
# расшифровка ключей, выбор режима/хоста, кэш — всё настоящее.

# Снято вручную: GET https://open-api.bingx.com/openApi/swap/v2/quote/contracts
# 2026-09-11, запись для symbol="BTC-USDT". price_precision/quantity_precision/
# min_quantity/min_notional — поля pricePrecision/quantityPrecision/
# tradeMinQuantity/tradeMinUSDT из ответа биржи (влияют на округление объёма,
# выдумывать их нельзя). max_leverage поля больше нет вовсе (раздел 16 ТЗ,
# шаг 15.5.1): BingX для этого контракта не отдаёт maxLongLeverage, а
# get_symbols() (app/exchanges/bingx.py) раньше молча подставляла дефолт 20 —
# убрано как тихий фолбэк; реальный источник максимума плеча теперь
# BingXClient.get_leverage(), не эта ручка.
_FAKE_SYMBOL_INFO = SymbolInfo(
    symbol="BTC-USDT",
    price_precision=1,
    quantity_precision=4,
    min_quantity=D("0.0001"),
    min_notional=D("2"),
)
_FAKE_PRICE = D("100250")  # середина зоны входа сигнала ниже — дрейф ровно 0%
_FAKE_BALANCE = D("10000")


async def _fake_get_ticker(self, symbol: str, *, max_retries=None) -> Ticker:  # type: ignore[no-untyped-def]
    return Ticker(
        symbol=symbol, last_price=_FAKE_PRICE, volume_24h=D("0"),
        price_change_percent=D("0"), timestamp=datetime.now(UTC),
    )


async def _fake_get_symbols(self, *, max_retries=None) -> list[SymbolInfo]:  # type: ignore[no-untyped-def]
    return [_FAKE_SYMBOL_INFO]


async def _fake_get_balance(self, *, max_retries=None) -> Balance:  # type: ignore[no-untyped-def]
    return Balance(
        asset="USDT", available=_FAKE_BALANCE, used_margin=D("0"),
        unrealized_pnl=D("0"), equity=_FAKE_BALANCE,
    )


async def _fake_get_position_mode(self, *, max_retries=None) -> bool:  # type: ignore[no-untyped-def]
    # True = hedge mode. Тот же дефолт, что демо-аккаунт BingX отдаёт в
    # реальной проверке раздела 16 ТЗ (recon, 2026-09) — не выдумка, а
    # согласованная во всех фейках этого шага величина.
    return True


async def _fake_get_positions(self, *, max_retries=None) -> list[Position]:  # type: ignore[no-untyped-def]
    # Позиций на бирже нет — как в живом прогоне (a) шага 15.5.4а: пустой список.
    return []


async def _seed_execution_fixtures(  # type: ignore[no-untyped-def]
    db: Database, settings
) -> tuple[SignalRecord, SignalNotification]:
    """SignalRecord (READY/ACTIVE) + снимок его уведомления (шаг 15.5.2а:
    кнопки адресуют notification_id) + фейковые ключи биржи, тем же путём, что
    и бот: repository + SecretCipher.encrypt (не сырой INSERT) — расшифровка
    в ExchangeFactory.for_user() должна реально отработать, не просто найти
    непустую строку в базе."""
    cipher = SecretCipher(settings.encryption_key.get_secret_value())
    async with db.session() as session:
        user = await UserRepository(session).get_by_telegram_id(USER_ID)
        assert user is not None, "ожидали пользователя smoke-теста — /start уже прошёл"

        creds = ExchangeCredentials(
            user_id=user.id, exchange="bingx", mode=ExchangeKeyMode.DEMO,
            is_read_only=False, is_active=True,
            # Раздел 8 ТЗ: отметка сразу свежая — иначе _build_quote() при
            # check_permissions=True (открытие карточки) сходит за
            # get_api_restrictions() в РЕАЛЬНЫЙ BingXClient (не патчится
            # ниже вместе с get_ticker/get_symbols/get_balance) с фейковым
            # ключом. Сети в смоук-тесте нет и не будет.
            permissions_checked_at=datetime.now(UTC),
        )
        creds.api_key_encrypted = cipher.encrypt("smoke-test-fake-api-key")
        creds.api_secret_encrypted = cipher.encrypt("smoke-test-fake-api-secret")
        creds.api_key_masked = mask_secret("smoke-test-fake-api-key")
        session.add(creds)

        signal = SignalRecord(
            user_id=user.id,
            symbol="BTC-USDT",
            timeframe="4h",
            level=SignalLevel.READY,
            status=SignalRecordStatus.ACTIVE,
            direction=SignalDirection.LONG,
            setup="Пробой с ретестом",
            fingerprint="smoke-check-fingerprint",
            detail="Сигнал для execution-сценария smoke_check.py.",
            entry_low=D("100000"),
            entry_high=D("100500"),
            stop_loss=D("99000"),
            take_profit=D("103000"),
            confidence=8,
            expires_at=datetime.now(UTC) + timedelta(hours=4),
        )
        session.add(signal)
        await session.flush()
        now = datetime.now(UTC)
        notification = SignalNotification.snapshot_of(
            signal, notified_at=now, expires_at=now + timedelta(hours=4)
        )
        session.add(notification)
        await session.flush()
        return signal, notification


async def _run_execution_scenario(sim, tg, db, redis, settings) -> None:  # type: ignore[no-untyped-def]
    # Гвард MODE_NOT_ALLOWED (execution.py) требует user.settings.active_exchange_mode
    # == settings.bingx_allowed_exchange_mode (DEMO по умолчанию — BINGX_TRADING_MODE
    # =demo). Дефолт нового пользователя — LIVE, переключаем тем же тапом, что и
    # пользователь: кнопка "💱 Счёт: ..." в «Настройках» (SetCB.MODE).
    await sim.send("/start")
    await sim.tap("Настройки")
    await sim.tap("Счёт")

    signal, notification = await _seed_execution_fixtures(db, settings)
    signal_id = signal.id
    notification_id = notification.id

    # Раздел "общие ключи BingX": ровно одна пара сохранена (DEMO, только
    # что засеяна выше) — экран "Ключи" обязан показать её как ОДИН ключ
    # на оба счёта, а не как раздельные LIVE/DEMO. См. app/bot/handlers/
    # settings.py::_show_api_keys_menu/_api_keys_menu (shared=True).
    await sim.send("/start")
    await sim.tap("Настройки")
    text = await sim.tap("Ключи")
    check(
        "Ключи BingX: общая пара — одна строка про ключ",
        has(text, "ключ:") and "обслуживает" in text.lower(),
        text[:300],
    )
    check(
        "Ключи BingX: нет второй строки «Демо: не подключены»",
        "демо:" not in text.lower(),  # не сама фраза "и демо-счёт" в пояснении, а строка-кредит
        text[:300],
    )
    buttons = sim.available_buttons()
    check(
        "Ключи BingX: кнопки без суффикса режима",
        "✏️ Заменить ключ" in buttons and "🔍 Проверить права" in buttons,
        str(list(buttons)),
    )
    check(
        "Ключи BingX: есть кнопка «Отдельный ключ для…»",
        any("отдельный ключ" in b.lower() for b in buttons),
        str(list(buttons)),
    )

    originals = (
        BingXClient.get_ticker,
        BingXClient.get_symbols,
        BingXClient.get_balance,
        BingXClient.get_position_mode,
        BingXClient.get_positions,
    )
    BingXClient.get_ticker = _fake_get_ticker
    BingXClient.get_symbols = _fake_get_symbols
    BingXClient.get_balance = _fake_get_balance
    BingXClient.get_position_mode = _fake_get_position_mode
    BingXClient.get_positions = _fake_get_positions
    try:
        text = await sim.tap_data(f"exn:open:{notification_id}")
        check("карточка: объём", has(text, "объём"), text[:300])
        check("карточка: риск", has(text, "риск"), text[:300])
        check("карточка: RR", has(text, "rr"), text[:300])

        text = await sim.tap_data(f"exn:yes:{notification_id}")
        # "Да" отправляет ДВА сообщения (execution.py:478-483): edit_text
        # "Подтверждено" на самой карточке, затем отдельным send детали
        # ордера ("Сухой прогон: ушёл бы..."). tap_data() возвращает только
        # последнее, поэтому первое проверяем по tg.log.
        edit_text = tg.log[-2][1]
        check("подтверждение: карточка помечена", has(edit_text, "подтверждено"), edit_text[:120])
        check("подтверждение: детали сухого прогона", has(text, "сухой прогон"), text[:200])

        async with db.session() as session:
            rows = (
                await session.scalars(
                    select(ExecutionOrder).where(ExecutionOrder.signal_id == signal_id)
                )
            ).all()
        check(
            "execution_orders: 3 строки DRY_RUN",
            len(rows) == 3 and all(r.status.value == "DRY_RUN" for r in rows),
            f"{len(rows)} строк, статусы {[r.status.value for r in rows]}",
        )

        # Второе "Да" держим за руку: лок ставит тот же RedisLock/ключ, что
        # и хендлер (confirm_lock_key), но берём его снаружи ДО тапа — так
        # результат детерминирован (не зависит от того, как event loop
        # чередует две по-настоящему параллельные корутины), а код хендлера
        # исполняется настоящий: confirm_yes реально получит LockBusyError.
        lock_key = confirm_lock_key(user_id=signal.user_id, notification_id=notification_id)
        blocker = RedisLock(redis, lock_key, ttl_seconds=15)
        await blocker.__aenter__()
        try:
            await sim.tap_data(f"exn:yes:{notification_id}")
        finally:
            await blocker.__aexit__(None, None, None)

        last_alert = next((entry for entry in reversed(tg.log) if entry[0] == "alert"), None)
        check(
            "повторное «Да»: отказ по локу, не трейс",
            last_alert is not None and "уже обрабатывается" in last_alert[1].lower(),
            str(last_alert),
        )
    finally:
        (
            BingXClient.get_ticker,
            BingXClient.get_symbols,
            BingXClient.get_balance,
            BingXClient.get_position_mode,
            BingXClient.get_positions,
        ) = originals


if __name__ == "__main__":
    asyncio.run(main())
