"""Точка входа.

Порядок запуска намеренно строгий:
  1. настройки          — падаем сразу, если окружение неполное;
  2. логирование        — с фильтром секретов, до любых других логов;
  3. проверка БД        — падаем до старта polling, а не в первом хендлере;
  4. справочники        — системные типы ошибок, идемпотентно;
  5. сборка бота        — зависимости через workflow_data;
  6. polling            — с корректным закрытием ресурсов в finally.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands
from redis.asyncio import Redis

from app.analysis.ai.provider import AnthropicClient
from app.bot.handlers import (
    analysis,
    common,
    diagnostics,
    exchange,
    exchange_menu,
    execution,
    fsm_guard,
    insights,
    pending,
    statistics,
    trades,
)
from app.bot.handlers import settings as settings_handlers
from app.bot.middlewares.access import AccessMiddleware
from app.bot.middlewares.ai_service import AIServiceMiddleware
from app.bot.middlewares.database import DatabaseMiddleware
from app.bot.middlewares.errors import ErrorMiddleware
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, setup_logging
from app.core.security import SecretCipher
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.services.user_service import UserService
from app.workers.scheduler import BackgroundJobs

logger = get_logger(__name__)

# Список должен точно соответствовать хендлерам Command(...) в app/bot/handlers —
# несуществующая команда в выпадающем списке хуже, чем её отсутствие.
BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Список команд"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="trades", description="История сделок"),
    BotCommand(command="open", description="Открытые позиции"),
    BotCommand(command="trade", description="Добавить сделку"),
    BotCommand(command="risk", description="Калькулятор позиции"),
    BotCommand(command="signal", description="Поиск точки входа"),
    BotCommand(command="analysis", description="Анализ рынка"),
    BotCommand(command="balance", description="Баланс на бирже"),
    BotCommand(command="prices", description="Цены инструментов"),
    BotCommand(command="import", description="Импорт сделок с биржи"),
    BotCommand(command="today", description="Отчёт за день"),
    BotCommand(command="week", description="Отчёт за неделю"),
    BotCommand(command="month", description="Отчёт за месяц"),
    BotCommand(command="mistakes", description="Анализ ошибок"),
    BotCommand(command="plan", description="Торговый план"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="ping", description="Проверка базы данных"),
]


async def setup_bot_ui(bot: Bot) -> None:
    """Кнопка меню и список команд рядом с полем ввода.

    Без этого пользователь каждый раз набирает /start вручную — кнопка
    открывает то же меню в один тап.
    """
    await bot.set_my_commands(BOT_COMMANDS)
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def build_dispatcher(
    settings: Settings, db: Database, llm_client: AnthropicClient | None, redis: Redis
) -> Dispatcher:
    """Собирает диспетчер: зависимости, middlewares, роутеры.

    FSM-хранилище пока в памяти. При переходе на несколько воркеров или
    при требовании переживать рестарт незавершённых форм — заменить на
    Redis; интерфейс aiogram при этом не меняется.
    """
    dp = Dispatcher(storage=MemoryStorage())

    dp["db"] = db
    dp["settings"] = settings
    dp["cipher"] = SecretCipher(settings.encryption_key.get_secret_value())
    # Этап 15, раздел 8: блокировка от двойного нажатия "Да" — отдельный
    # клиент от FSM-хранилища выше, назначение другое (см. app/core/locks.py).
    dp["redis"] = redis

    # Порядок критичен:
    #   access     — отсекаем чужих до любых запросов в базу;
    #   errors     — ловим исключения своих хендлеров;
    #   database   — открывает транзакцию последней, чтобы она была самой
    #                короткой и не висела на времени проверки доступа;
    #   ai_service — идёт после database: ему нужна уже открытая сессия
    #                запроса, чтобы собрать сервис в той же транзакции.
    for observer in (dp.message, dp.callback_query):
        observer.middleware(AccessMiddleware(settings.allowed_ids))
        observer.middleware(ErrorMiddleware())
        observer.middleware(DatabaseMiddleware(db, settings))
        observer.middleware(AIServiceMiddleware(llm_client, settings))

    # fsm_guard идёт первым: он перехватывает команды, введённые посреди
    # формы, сбрасывает состояние и передаёт сообщение дальше.
    dp.include_router(fsm_guard.router)
    dp.include_router(common.router)
    dp.include_router(diagnostics.router)
    dp.include_router(trades.router)
    dp.include_router(analysis.router)
    dp.include_router(exchange_menu.router)
    dp.include_router(exchange.router)
    dp.include_router(statistics.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(insights.router)
    dp.include_router(execution.router)
    dp.include_router(pending.router)

    return dp


async def bootstrap_reference_data(db: Database, settings: Settings) -> None:
    """Наполняет общие справочники. Идемпотентно."""
    async with db.session() as session:
        service = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        await service.ensure_system_mistakes()


async def run() -> None:
    settings = get_settings()

    setup_logging(
        level=settings.log_level,
        json_output=settings.log_json,
        secrets=settings.secret_values,
    )
    logger.info(
        "Запуск приложения",
        extra={"environment": settings.environment, "tz": settings.default_timezone},
    )

    db = Database(settings)
    try:
        await db.healthcheck()
        logger.info("Соединение с PostgreSQL установлено")
        await bootstrap_reference_data(db, settings)
    except Exception:
        logger.exception("Не удалось инициализировать базу данных. Остановка.")
        await db.dispose()
        raise

    # Прокси нужен там, где хостинг режет трафик до IP Telegram — типичная
    # ситуация в российских дата-центрах с DPI. Без прокси бот уходит в
    # бесконечный цикл рестартов с TelegramNetworkError.
    session: AiohttpSession | None = None
    if settings.proxy_url:
        session = AiohttpSession(proxy=settings.proxy_url)
        logger.info("Telegram подключается через прокси")

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )

    llm_client: AnthropicClient | None = None
    if settings.ai_enabled and settings.anthropic_api_key:
        llm_client = AnthropicClient(
            settings.anthropic_api_key.get_secret_value(),
            model=settings.ai_model,
            base_url=settings.ai_base_url,
            auth_scheme=settings.ai_auth_scheme,
        )
    else:
        logger.warning("AI-слой выключен: AI_ENABLED=false или не задан ANTHROPIC_API_KEY")

    # Только для блокировки от двойного нажатия (этап 15, раздел 8) —
    # соединение ленивое, падать здесь не на чем, пока не понадобится ключ.
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    dp = build_dispatcher(settings, db, llm_client, redis)

    if not settings.allowed_ids:
        logger.warning(
            "ALLOWED_TELEGRAM_IDS не задан — бот отвечает любому пользователю"
        )

    # Отдельный SecretCipher, не тот, что в dp["cipher"]: фоновые задачи не
    # зависят от build_dispatcher, а сам шифр не хранит состояния — второй
    # экземпляр с тем же ключом безвреден.
    background_jobs = BackgroundJobs(
        bot, db, settings, SecretCipher(settings.encryption_key.get_secret_value())
    )
    background_jobs.start()

    try:
        await setup_bot_ui(bot)
        # Сбрасываем накопившиеся за простой апдейты: обрабатывать команды
        # недельной давности бессмысленно и потенциально вредно.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        logger.info("Остановка приложения")
        await background_jobs.shutdown()
        if llm_client is not None:
            await llm_client.aclose()
        await bot.session.close()
        await redis.aclose()
        await db.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Получен сигнал остановки")


if __name__ == "__main__":
    main()
