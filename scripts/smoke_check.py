"""Полная проверка бота перед выкаткой.

Проходит все разделы меню и все команды, отмечая проблемы. Задача —
поймать то, что не видят юнит-тесты: пустые ответы, необработанные
кнопки, застрявшие состояния, ошибки в связках между разделами.

Запуск: python -m scripts.smoke_check
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any, NoReturn

sys.path.insert(0, ".")

import aiohttp
import httpx
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.security import SecretCipher, mask_secret
from app.database.models.credentials import ExchangeCredentials
from app.database.models.execution_callback import ExecutionCallback
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    Balance,
    CommissionRate,
    LeverageInfo,
    MarginType,
    OpenOrder,
    Position,
    SymbolInfo,
    Ticker,
)
from app.exchanges.bingx import BingXClient
from app.trading.enums import ExchangeKeyMode, TradeSide
from scripts.simulate_chat import USER_ID, build  # noqa: E402

# Скрипт пишет и удаляет реальные строки — только тестовая база. Имя, а не
# просто "не совпадает с проду по случайности": DATABASE_URL может указывать
# на что угодно, включая прод по опечатке в окружении запуска.
REQUIRED_DB_NAME = "trading_bot_test"

problems: list[str] = []
checks = {"total": 0, "passed": 0}

# --- Запрет сети --------------------------------------------------------------
#
# Заглушки методов BingXClient (раздел [14] ниже) закрывают только известные
# вызовы: новый метод без заглушки уходил на биржу с фейковым ключом (так было
# с get_positions на 15.5.4а, BingX ответил 100413). Поэтому сеть запрещена
# целиком на уровне транспорта, классом, а не экземпляром: любой клиент,
# созданный где угодно (ExchangeFactory.public_client()/for_user(),
# AI-провайдер, aiogram), и до установки запрета тоже.
#
# Исключение намеренно не httpx.HTTPError: BingXClient._request превратил бы
# его в ExchangeUnavailableError с повторами, и хендлер показал бы «биржа
# недоступна» вместо причины. Хендлер или middleware всё равно могут поймать
# исключение — поэтому каждая попытка сначала пишется в blocked_requests, и
# main() по непустому списку завершается с exit 1 независимо от проверок.


class NetworkBlockedError(RuntimeError):
    """Сетевой запрос из smoke_check — запрещён."""


blocked_requests: list[str] = []
_BOT_TOKEN_IN_URL = re.compile(r"/bot[^/]+/")


def _block(method: str, url: object) -> NoReturn:
    # URL Bot API несёт токен в пути (/bot<token>/getMe) — в вывод не пускаем.
    line = f"{method} {_BOT_TOKEN_IN_URL.sub('/bot<token>/', str(url))}"
    blocked_requests.append(line)
    print(f"  ✗ СЕТЬ: {line}")
    raise NetworkBlockedError(f"Сеть запрещена в smoke_check: {line}")


async def _blocked_httpx_async(self: Any, request: httpx.Request) -> NoReturn:
    _block(request.method, request.url)


def _blocked_httpx_sync(self: Any, request: httpx.Request) -> NoReturn:
    _block(request.method, request.url)


async def _blocked_aiohttp(
    self: Any, method: str, str_or_url: object, *args: Any, **kwargs: Any
) -> NoReturn:
    _block(method, str_or_url)


_NETWORK_GUARDS: tuple[tuple[type, str, Any], ...] = (
    (httpx.AsyncHTTPTransport, "handle_async_request", _blocked_httpx_async),
    (httpx.HTTPTransport, "handle_request", _blocked_httpx_sync),
    # Нижний уровень aiohttp: через него идут и make_request, и
    # stream_content у aiogram AiohttpSession. Приватный API — отсюда
    # проверка существования ниже.
    (aiohttp.ClientSession, "_request", _blocked_aiohttp),
)


def _install_network_guard() -> None:
    """Проверка существования — явным if, не assert: assert снимается
    под python -O, а setattr на отсутствующий атрибут молча создал бы
    новый, оставив настоящий путь в сеть открытым (после переименования
    в новой версии библиотеки)."""
    for owner, name, _ in _NETWORK_GUARDS:
        if not callable(getattr(owner, name, None)):
            print(
                f"Отказ: {owner.__module__}.{owner.__qualname__}.{name} не найден — "
                "запрет сети не встанет. Версия библиотеки сменила внутренний API; "
                "поправь _NETWORK_GUARDS."
            )
            sys.exit(1)
    for owner, name, blocked in _NETWORK_GUARDS:
        setattr(owner, name, blocked)


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
    """Smoke гоняется в конфигурации прода — с включённым исполнением.
    Вход по сигналу удалён 02.10.2026; карточки управления позициями
    (этап 4) вернут сюда сценарий, которому флаг нужен, — требование
    оставлено, чтобы прогоны до и после были сравнимы."""
    if not execution_enabled:
        print(
            "Отказ: TRADING_EXECUTION_ENABLED не включён — smoke гоняется в "
            "конфигурации прода. Запусти с TRADING_EXECUTION_ENABLED=true."
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


async def _set_journal_cutoff(db, value: datetime | None) -> None:  # type: ignore[no-untyped-def]
    async with db.session() as session:
        user = await UserRepository(session).get_by_telegram_id(USER_ID)
        settings_row = await UserRepository(session).get_settings(user.id)
        settings_row.journal_cutoff_at = value
        await session.commit()


async def add_trade(sim, symbol, side, entry, sl, tp, balance="10000"):  # type: ignore[no-untyped-def]
    await sim.send("/start")
    await sim.tap("Добавить сделку")
    # 05.10.2026: в начале — выбор пути; журнал — прежний мастер.
    await sim.tap("Только записать в журнал")
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
    await sim.tap("Позиции")
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
    _install_network_guard()

    sim, tg, db, redis = await build()
    # 03.10.2026: мастер сделки берёт шаг лота из публичного списка контрактов
    # (объём от риска вниз до шага) — заглушка на весь прогон.
    BingXClient.get_symbols = _fake_contracts  # type: ignore[method-assign]

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
    if blocked_requests:
        print(f"\nЗАБЛОКИРОВАНЫ СЕТЕВЫЕ ЗАПРОСЫ ({len(blocked_requests)}):")
        for line in blocked_requests:
            print(f"  • {line}")
        print("Код smoke_check дошёл до сети — нужна заглушка или это дефект.")
    if problems:
        print(f"\nПРОБЛЕМЫ ({len(problems)}):")
        for p in problems:
            print(f"  • {p}")
        # check() только копит `problems` и печатает "✗" — без этого exit
        # code всегда 0, и упавшая проверка (например, execution_orders не
        # DRY_RUN) тонет в выводе, а не останавливает деплой/CI.
        sys.exit(1)
    if blocked_requests:
        sys.exit(1)
    print("Проблем не найдено.")


async def _run_scenarios(sim, tg, db, redis, settings) -> None:  # type: ignore[no-untyped-def]
    print("\n[1] Базовые команды")
    text = await sim.send("/start")
    check("/start открывает меню", has(text, "торговый журнал", "выбери раздел"), text[:60])
    check("меню содержит все разделы", len(sim.available_buttons()) == 11,
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
        ("Статистика", "сделок"), ("Позиции", "нет"),
        ("Сделки", "нет"), ("Просадка", "нет"),
    ]:
        await sim.send("/start")
        text = await sim.tap(label)
        check(f"«{label}» на пустой базе", len(text) > 10, "пустой ответ")

    # Этап 3: «Позиции» без ключей — только журнал, честная причина, на биржу
    # не ходит (запрет сети стоит на весь прогон).
    await sim.send("/start")
    text = await sim.tap("Позиции")
    check("«Позиции» без ключей объясняют", has(text, "ключи биржи не подключены"), text[:200])

    # «Ошибки» убрали из меню (дублировала «Анализ ошибок»), но команда
    # /mistakes с числовой статистикой по-прежнему работает.
    text = await sim.send("/mistakes")
    check("«/mistakes» на пустой базе", len(text) > 10, "пустой ответ")

    print("\n[3] Добавление сделки")
    first = len(tg.created)
    text = await add_trade(sim, "BTC", "LONG", "100500", "99500", "102500")
    check("сделка сохраняется", has(text, "сделка записана"), text[:80])
    check("объём рассчитан", has(text, "объём: 0.2"), text[:200])
    check("риск верный", has(text, "риск: 2.00%"), text[:200])
    # 03.10.2026: переписка мастера удалена; остались «/start» пользователя
    # (до формы) и карточка сделки. 10.10.2026 (A.1): экран меню, с которого
    # начат мастер, — не его сообщение: не правится и не удаляется, первый
    # шаг — новым сообщением.
    menu_id = tg.created[first + 1]
    wizard = set(tg.created[first + 2:]) - {tg.last_message_id}
    left = sorted(wizard - set(tg.deleted))
    check("переписка мастера удалена", not left and tg.last_message_id not in tg.deleted,
          f"не удалены: {left}")
    check("меню, с которого начат мастер, не удалено", menu_id not in tg.deleted,
          f"удалено {menu_id}")
    check("меню, с которого начат мастер, не правилось", menu_id not in tg.edited_ids,
          f"правилось {menu_id}")

    # M4: отсечка журнала видна в «Настройках» (местное время, метка пояса).
    await _set_journal_cutoff(db, datetime(2026, 10, 3, 20, 38, 34, tzinfo=UTC))
    await sim.send("/start")
    text = await sim.tap("Настройки")
    check("отсечка журнала в настройках",
          has(text, "журнал ведётся с 04.10.2026 01:38 (utc+5)"), text[:400])
    await _set_journal_cutoff(db, None)
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
    text = await add_trade(sim, "ETH", "LONG", "3000", "2940", "3120")
    # 200 / 60 = 3.333… → вниз до шага лота ETH 0.01, не 3.33333333.
    check("объём от риска вниз до шага лота", has(text, "объём: 3.33")
          and "3.3333" not in text, text[:200])
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

    # Этап 5: приближение к стопу/тейку — раздельно, порог на пользователя.
    await sim.send("/start")
    await sim.tap("Настройки")
    text = await sim.tap("Уведомления")
    buttons = list(sim.available_buttons())
    check("уведомления: стоп и тейк раздельно",
          any("приближение к стопу" in b for b in buttons)
          and any("приближение к тейку" in b for b in buttons)
          and not any("TP/SL" in b for b in buttons), str(buttons))
    check("уведомления: порог по умолчанию 80%",
          any("Порог стопа: 80%" in b for b in buttons), str(buttons))
    await sim.tap("Порог стопа")
    await sim.tap("85%")
    buttons = list(sim.available_buttons())
    check("порог стопа меняется", any("Порог стопа: 85%" in b for b in buttons), str(buttons))

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
    await sim.tap("Только записать в журнал")
    await sim.tap("BTC")
    text = await sim.send("/stats")
    check("команда не попадает в форму", "направление" not in text.lower(), text[:120])
    check("форма отменяется с пояснением", "отменено" in text.lower(), text[:120])
    text = await sim.send("/stats")
    check("повторная команда работает", "win rate" in text.lower(), text[:120])

    print("\n[11] Некорректный ввод")
    await sim.send("/start")
    await sim.tap("Добавить сделку")
    await sim.tap("Только записать в журнал")
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
    # Сигналы удалены 02.10.2026: экран — информация, не рекомендация.
    check("пометка «не торговая рекомендация»", has(text, "не торговая рекомендация"), text[:300])
    await sim.send("/start")
    buttons = list(sim.available_buttons())
    check("в меню нет «Найти вход»", not any("найти вход" in b.lower() for b in buttons),
          str(buttons))
    text = await sim.send("/help")
    check("/help без /signal", "/signal" not in text, text[:300])
    text = await sim.tap_data("menu:find_entry")
    check("старая кнопка «Найти вход» отвечает", has(text, "сигналы отключены"), text[:150])

    print("\n[14] Ключи биржи и старые кнопки сигналов")
    await _run_execution_scenario(sim, tg, db, redis, settings)

    print("\n[15] Действия с позицией (этап 4)")
    await _run_position_actions(sim, tg, db, settings)

    print("\n[16] Открытие сделки из бота (сухой прогон)")
    await _run_open_trade(sim, tg, db, settings)


# --- [14] Ключи биржи и старые кнопки сигналов --------------------------------
#
# Вход по сигналу удалён 02.10.2026. Под старыми уведомлениями в чате остались
# кнопки «exn:…» — они обязаны ответить «Сигналы отключены», ничего не записав
# в базу и не сходив на биржу (запрет сети стоит на весь прогон).


async def _seed_exchange_credentials(db: Database, settings) -> int:  # type: ignore[no-untyped-def]
    """Фейковые ключи биржи тем же путём, что и бот: repository +
    SecretCipher.encrypt (не сырой INSERT). Возвращает user_id."""
    cipher = SecretCipher(settings.encryption_key.get_secret_value())
    async with db.session() as session:
        user = await UserRepository(session).get_by_telegram_id(USER_ID)
        assert user is not None, "ожидали пользователя smoke-теста — /start уже прошёл"

        creds = ExchangeCredentials(
            user_id=user.id, exchange="bingx", mode=ExchangeKeyMode.DEMO,
            is_read_only=False, is_active=True,
            # Отметка сразу свежая — экран ключей не идёт за правами на биржу.
            permissions_checked_at=datetime.now(UTC),
        )
        creds.api_key_encrypted = cipher.encrypt("smoke-test-fake-api-key")
        creds.api_secret_encrypted = cipher.encrypt("smoke-test-fake-api-secret")
        creds.api_key_masked = mask_secret("smoke-test-fake-api-key")
        session.add(creds)
        await session.flush()
        return user.id


async def _run_execution_scenario(sim, tg, db, redis, settings) -> None:  # type: ignore[no-untyped-def]
    user_id = await _seed_exchange_credentials(db, settings)

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

    for data in ("exn:open:1", "exn:yes:1", "exn:no:1"):
        await sim.tap_data(data)
        last_alert = next((entry for entry in reversed(tg.log) if entry[0] == "alert"), None)
        check(
            f"старая кнопка {data.rsplit(':', 1)[0]}: «Сигналы отключены»",
            last_alert is not None and "сигналы отключены" in last_alert[1].lower(),
            str(last_alert),
        )

    async with db.session() as session:
        presses = (
            await session.scalars(
                select(ExecutionCallback).where(ExecutionCallback.user_id == user_id)
            )
        ).all()
        orders = (
            await session.scalars(
                select(ExecutionOrder).where(ExecutionOrder.user_id == user_id)
            )
        ).all()
    check(
        "старые кнопки ничего не пишут в базу",
        not presses and not orders,
        f"нажатий {len(presses)}, ордеров {len(orders)}",
    )


# --- [15] Действия с позицией (этап 4) -----------------------------------------
#
# Путь кнопки: экран «Позиции» → карточка → «Да». Биржа — заглушки leaf-методов
# BingXClient (как в бывшем [14] входа); ExchangeFactory, проверки, Redis-лок,
# журнал нажатий, БД — настоящие. EXEC_DRY_RUN по умолчанию true — «Да» пишет
# DRY_RUN и на биржу не ходит; метод без заглушки упрётся в запрет сети.

_XRP = SymbolInfo("XRP-USDT", 4, 0, D(2), D(2))


async def _fake_positions(self, *, max_retries=None) -> list[Position]:  # type: ignore[no-untyped-def]
    return [Position(
        symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(30), entry_price=D("1.5253"),
        mark_price=D("1.56"), leverage=20, unrealized_pnl=D("1.04"),
        liquidation_price=D("1.4553"), position_id="2105907655281221634",
    )]


async def _fake_open_orders(self, symbol=None, *, max_retries=None) -> list[OpenOrder]:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    return [OpenOrder(
        order_id="2105910661355233280", client_order_id="", symbol="XRP-USDT", side="SELL",
        position_side="LONG", order_type="STOP_MARKET", quantity=D(40), executed_qty=D(0),
        price=D(0), stop_price=D("1.4795"), status="NEW", leverage=20, reduce_only=True,
        close_position=True, working_type="MARK_PRICE", created_at=now, updated_at=now,
        take_profit=None, stop_loss=None,
    )]


async def _fake_mark(self, symbol: str) -> D:  # type: ignore[no-untyped-def]
    return D("1.56")


async def _fake_symbols(self, *, max_retries=None) -> list[SymbolInfo]:  # type: ignore[no-untyped-def]
    return [_XRP]


async def _fake_contracts(self, *, max_retries=None) -> list[SymbolInfo]:  # type: ignore[no-untyped-def]
    return [_XRP, SymbolInfo("BTC-USDT", 1, 4, D("0.0001"), D(2)),
            SymbolInfo("ETH-USDT", 2, 2, D("0.01"), D(2))]


async def _fake_balance(self, *, max_retries=None) -> Balance:  # type: ignore[no-untyped-def]
    return Balance("USDT", D(9000), D(100), D(0), D(10000))


async def _fake_position_mode(self, *, max_retries=None) -> bool:  # type: ignore[no-untyped-def]
    return True


_STUBS = {
    "get_positions": _fake_positions,
    "get_open_orders": _fake_open_orders,
    "get_mark_price": _fake_mark,
    "get_symbols": _fake_symbols,
    "get_balance": _fake_balance,
    "get_position_mode": _fake_position_mode,
}


async def _run_position_actions(sim, tg, db, settings) -> None:  # type: ignore[no-untyped-def]
    # Действия разрешены на счёте bingx_allowed_exchange_mode (демо по
    # умолчанию) — переключаем счёт тем же тапом, что пользователь.
    await sim.send("/start")
    await sim.tap("Настройки")
    await sim.tap("Счёт")
    originals = {name: getattr(BingXClient, name) for name in _STUBS}
    for name, fake in _STUBS.items():
        setattr(BingXClient, name, fake)
    try:
        await sim.send("/start")
        text = await sim.tap("Позиции")
        check("«Позиции»: позиция с биржи", has(text, "XRP-USDT", "на всю позицию"), text[:300])
        buttons = list(sim.available_buttons())
        check("«Позиции»: кнопка позиции, действий в списке нет",
              "⚙️ XRP LONG" in buttons and not any("безубыток" in b for b in buttons),
              str(buttons))

        text = await sim.tap("⚙️ XRP LONG")
        buttons = list(sim.available_buttons())
        check("экран действий позиции", has(text, "Действия · XRP-USDT LONG")
              and "🛡 Стоп в безубыток" in buttons and "◀️ К позициям" in buttons, str(buttons))

        text = await sim.tap("Стоп в безубыток")
        check("карточка безубытка", has(text, "стоп в безубыток", "не торговая рекомендация"),
              text[:300])
        text = await sim.tap("Да")
        check("«Да» в сухом прогоне", has(text, "сухой прогон"), text[:200])

        async with db.session() as session:
            user = await UserRepository(session).get_by_telegram_id(USER_ID)
            actions = list(await session.scalars(
                select(PositionAction).where(PositionAction.user_id == user.id)
                .order_by(PositionAction.id)
            ))
            orders = list(await session.scalars(
                select(ExecutionOrder).where(ExecutionOrder.position_action_id.is_not(None))
                .where(ExecutionOrder.user_id == user.id)
            ))
            presses = [p.action for p in await session.scalars(
                select(ExecutionCallback).where(ExecutionCallback.user_id == user.id)
                .order_by(ExecutionCallback.id)
            )]
        check("действие: DRY_RUN",
              [a.status.value for a in actions] == ["DRY_RUN"], str([a.status for a in actions]))
        check("ордер действия: DRY_RUN на всю позицию",
              len(orders) == 1 and orders[0].status.value == "DRY_RUN"
              and orders[0].quantity == D(30), str([(o.status, o.quantity) for o in orders]))
        check("журнал нажатий: pm_open, pm_yes", presses == ["pm_open", "pm_yes"], str(presses))

        # Стоп дальше от входа — риск растёт: только «Да, увеличить риск».
        await sim.send("/start")
        await sim.tap("Позиции")
        await sim.tap("⚙️ XRP LONG")
        await sim.tap("Изменить стоп")
        # 03.10: вопрос о цене — отдельным сообщением с ForceReply и подсказкой.
        placeholder = tg.force_reply.input_field_placeholder if tg.force_reply else ""
        check("вопрос о цене с ForceReply и примером",
              placeholder.startswith("Цена стопа, например"), repr(placeholder))
        # Не число — ввод не сбрасывается, вопрос задаётся заново (03.10).
        text = await sim.send("Абв")
        check("«Абв» — вопрос о цене заново, ввод не сброшен",
              tg.asks[-1].startswith("✍️ Не принял") and "нужна цена числом" in text,
              f"{tg.asks[-1:]!r} {text[:120]!r}")
        text = await sim.send("1.40")
        check("карточка роста риска", has(text, "риск увеличится"), text[:300])
        buttons = list(sim.available_buttons())
        check("кнопка «Да, увеличить риск»", any("увеличить риск" in b for b in buttons),
              str(buttons))
        async with db.session() as session:
            last = await session.scalar(
                select(PositionAction).where(PositionAction.user_id == user.id)
                .order_by(PositionAction.id.desc())
            )
        text = await sim.tap_data(f"pm:y:{last.id}")
        check("обычное «Да» при росте риска отклонено", has(text, "увеличить риск"), text[:200])
    finally:
        for name, method in originals.items():
            setattr(BingXClient, name, method)


# --- [16] Открытие сделки из бота (05.10.2026) ---------------------------------
#
# «Добавить сделку» → «Открыть на бирже» → поиск монеты → … → карточка →
# «Открыть». EXEC_OPEN_DRY_RUN по умолчанию true — строки DRY_RUN, на биржу
# ничего. Биржа — заглушки leaf-методов BingXClient; метод без заглушки упрётся
# в запрет сети.


async def _fake_ticker(self, symbol: str, *, max_retries=None) -> Ticker:  # type: ignore[no-untyped-def]
    return Ticker(symbol, D("3000"), datetime.now(UTC))


async def _fake_leverage(self, symbol: str, *, max_retries=None) -> LeverageInfo:  # type: ignore[no-untyped-def]
    return LeverageInfo(symbol, 20, 20, 100, 100)


async def _fake_margin_type(self, symbol: str, *, max_retries=None) -> MarginType:  # type: ignore[no-untyped-def]
    return MarginType.ISOLATED


async def _fake_commission(self) -> CommissionRate:  # type: ignore[no-untyped-def]
    return CommissionRate(taker=D("0.0005"), maker=D("0.0002"))


_OPEN_STUBS = {
    **_STUBS,
    "get_symbols": _fake_contracts,
    "get_ticker": _fake_ticker,
    "get_leverage": _fake_leverage,
    "get_margin_type": _fake_margin_type,
    "get_commission_rate": _fake_commission,
}


async def _run_open_trade(sim, tg, db, settings) -> None:  # type: ignore[no-untyped-def]
    from app.bot.handlers.exchange import _market_cache

    _market_cache.invalidate()   # [15] положил в общий кэш контракты без ETH
    originals = {name: getattr(BingXClient, name) for name in _OPEN_STUBS}
    for name, fake in _OPEN_STUBS.items():
        setattr(BingXClient, name, fake)
    try:
        await sim.send("/start")
        await sim.tap("Добавить сделку")
        buttons = list(sim.available_buttons())
        check("«Добавить сделку»: выбор пути",
              "🟢 Открыть на бирже" in buttons and "📝 Только записать в журнал" in buttons,
              str(buttons))
        text = await sim.tap("Открыть на бирже")
        check("открытие: шаг монеты", has(text, "монета"), text[:200])
        text = await sim.send("eth")
        check("поиск монеты: eth → ETH-USDT", has(text, "ETH-USDT", "направление"), text[:200])
        await sim.tap("LONG")
        await sim.tap("По рынку")
        await sim.send("2940")
        await sim.send("3120")
        text = await sim.tap("По плану")
        check("шаг плеча: максимум при стопе", has(text, "при этом стопе"), text[:300])
        text = await sim.tap("предложено")
        check("карточка открытия",
              has(text, "Открыть на бирже — DEMO", "Риск:", "с комиссией", "Комиссия ≈",
                  "Ликвидация ≈", "Сухой прогон"), text[:600])
        text = await sim.tap("Открыть")
        check("«Открыть» в сухом прогоне", has(text, "сухой прогон: маркет ETH-USDT LONG"),
              text[:300])

        async with db.session() as session:
            user = await UserRepository(session).get_by_telegram_id(USER_ID)
            openings = list(await session.scalars(
                select(TradeOpening).where(TradeOpening.user_id == user.id)
            ))
            orders = list(await session.scalars(
                select(ExecutionOrder).where(ExecutionOrder.trade_opening_id.is_not(None))
                .where(ExecutionOrder.user_id == user.id)
            ))
            presses = [p.action for p in await session.scalars(
                select(ExecutionCallback).where(ExecutionCallback.user_id == user.id)
                .where(ExecutionCallback.trade_opening_id.is_not(None))
            )]
        check("открытие: DRY_RUN", [o.status.value for o in openings] == ["DRY_RUN"],
              str([o.status for o in openings]))
        check("ордера открытия: вход, стоп, тейк — DRY_RUN",
              sorted(o.role.value for o in orders) == ["ENTRY", "STOP_LOSS", "TAKE_PROFIT"]
              and {o.status.value for o in orders} == {"DRY_RUN"},
              str([(o.role, o.status) for o in orders]))
        check("журнал нажатий: to_yes", presses == ["to_yes"], str(presses))

        # Лимит и «Отмена» на карточке.
        await sim.send("/start")
        await sim.tap("Добавить сделку")
        await sim.tap("Открыть на бирже")
        await sim.send("ETH")
        await sim.tap("LONG")
        await sim.tap("Лимитный")
        await sim.send("2950")
        check("срок лимита: 4 ч по умолчанию", "4 ч ✓" in list(sim.available_buttons()),
              str(list(sim.available_buttons())))
        await sim.tap("4 ч")
        await sim.send("2900")
        await sim.tap("Без тейка")
        await sim.tap("По плану")
        text = await sim.tap("предложено")
        check("карточка лимита", has(text, "Лимит 2950 · срок 4 ч", "без тейка"), text[:300])
        text = await sim.tap("Отмена")
        check("«Отмена» на карточке", has(text, "ничего не отправлено"), text[:200])
    finally:
        for name, method in originals.items():
            setattr(BingXClient, name, method)


if __name__ == "__main__":
    asyncio.run(main())
