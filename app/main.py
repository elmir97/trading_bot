"""Точка входа.

Порядок запуска намеренно строгий:
  1. настройки          — падаем сразу, если окружение неполное;
  2. логирование        — с фильтром секретов, до любых других логов;
  3. проверка БД        — падаем до старта polling, а не в первом хендлере;
  4. справочники        — системные типы ошибок, идемпотентно;
  5. сборка бота        — зависимости через workflow_data;
  6. polling            — с корректным закрытием ресурсов в finally.

Остановка по SIGTERM (план 27.10): поллинг и планировщик стоп → обработчики и
циклы в полёте доживают (срок BOT_SHUTDOWN_DRAIN_SECONDS) → ресурсы. В логе —
«Остановка завершена за X с» (чек-лист деплоя, CLAUDE.md п.8).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands, MenuButtonWebApp, WebAppInfo
from pydantic import ValidationError
from redis.asyncio import Redis

from app.analysis.ai.provider import AnthropicClient
from app.bot.handlers import (
    analysis,
    common,
    diagnostics,
    exchange,
    exchange_menu,
    fsm_guard,
    insights,
    open_trade,
    pending,
    position_actions,
    positions,
    signals_retired,
    statistics,
    trades,
)
from app.bot.handlers import settings as settings_handlers
from app.bot.middlewares.access import AccessMiddleware
from app.bot.middlewares.ai_service import AIServiceMiddleware
from app.bot.middlewares.database import DatabaseMiddleware
from app.bot.middlewares.errors import ErrorMiddleware
from app.bot.middlewares.final_messages import FinalMessageMiddleware
from app.bot.prompts import PromptMiddleware
from app.bot.wizard_trail import WizardTrailMiddleware
from app.core.config import Settings, get_settings
from app.core.input_prompt import InputGate
from app.core.logging import get_logger, setup_logging
from app.core.security import SecretCipher
from app.core.shutdown import (
    BOT_SHUTDOWN_DRAIN_SECONDS,
    InFlight,
    InFlightMiddleware,
    StopSignal,
    close_all,
    drain,
    log_finished,
    poll_until_stopped,
)
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
    BotCommand(command="open", description="Позиции: биржа и журнал"),
    BotCommand(command="trade", description="Добавить сделку"),
    BotCommand(command="risk", description="Калькулятор позиции"),
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


MINIAPP_BUTTON_TEXT = "Торговля"


def menu_button(settings: Settings) -> MenuButtonCommands | MenuButtonWebApp:
    """MINIAPP_URL задан — кнопка меню открывает Mini App; пусто — список команд,
    как до Mini App (откат кнопки — пустое значение в override и рестарт)."""
    if settings.miniapp_url:
        return MenuButtonWebApp(text=MINIAPP_BUTTON_TEXT,
                                web_app=WebAppInfo(url=settings.miniapp_url))
    return MenuButtonCommands()


async def setup_bot_ui(bot: Bot, settings: Settings) -> None:
    """Кнопка меню и список команд рядом с полем ввода.

    Без этого пользователь каждый раз набирает /start вручную — кнопка
    открывает то же меню в один тап. Кнопка ставится при каждом старте и
    перекрывает настройку из BotFather — источник один: MINIAPP_URL.
    """
    await bot.set_my_commands(BOT_COMMANDS)
    button = menu_button(settings)
    await bot.set_chat_menu_button(menu_button=button)
    logger.info("Кнопка меню бота",
                extra={"kind": button.type, "url": settings.miniapp_url or None})


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
        # 10.10.2026 (A.1): кнопка под итоговым сообщением — экран новым
        # сообщением, итог не правится (app/bot/messaging.py).
        observer.middleware(FinalMessageMiddleware(db))
        observer.middleware(AIServiceMiddleware(llm_client, settings))
        # 03.10: числовой вопрос с ForceReply — убрать, когда шаг сменился,
        # задать снова, когда ответ не принят (app/bot/prompts.py).
        observer.middleware(PromptMiddleware())
        # 03.10: переписка мастера «Добавить сделку» убирается после записи
        # или отмены (app/bot/wizard_trail.py).
        observer.middleware(WizardTrailMiddleware())

    # fsm_guard идёт первым: он перехватывает команды, введённые посреди
    # формы, сбрасывает состояние и передаёт сообщение дальше.
    dp.include_router(fsm_guard.router)
    dp.include_router(common.router)
    dp.include_router(diagnostics.router)
    dp.include_router(positions.router)
    dp.include_router(position_actions.router)
    dp.include_router(open_trade.router)
    dp.include_router(trades.router)
    dp.include_router(analysis.router)
    dp.include_router(exchange_menu.router)
    dp.include_router(exchange.router)
    dp.include_router(statistics.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(insights.router)
    dp.include_router(signals_retired.router)
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
    try:
        settings = get_settings()
    except ValidationError as exc:
        # Запрещённая конфигурация (например EXEC_OPEN_FAULT при live) — отказ на
        # старте с внятной строкой в логе, а не молчаливый перезапуск.
        logging.basicConfig(level=logging.ERROR)
        logging.getLogger(__name__).error("Настройки отклонены, бот не запущен: %s", exc)
        raise SystemExit(2) from exc

    setup_logging(
        level=settings.log_level,
        json_output=settings.log_json,
        secrets=settings.secret_values,
    )
    logger.info(
        "Запуск приложения",
        extra={"environment": settings.environment, "tz": settings.default_timezone},
    )
    # Раздел 16 ТЗ, шаг 15.5.1: значение — не литерал, а расчёт по текущим
    # http_timeout_seconds/confirm_path_http_calls, увидеть его иначе,
    # кроме как в логе старта, нельзя.
    logger.info(
        "TTL лока подтверждения",
        extra={"confirm_lock_ttl_seconds": settings.confirm_lock_ttl_seconds},
    )

    # Свой обработчик SIGTERM/SIGINT с самого начала: сигнал во время
    # восстановления на старте не обрывает процесс без finally.
    stop = StopSignal()
    stop.install()

    db = Database(settings)
    try:
        await db.healthcheck()
        logger.info("Соединение с PostgreSQL установлено")
        await bootstrap_reference_data(db, settings)
    except Exception:
        logger.exception("Не удалось инициализировать базу данных. Остановка.")
        await db.dispose()
        stop.uninstall()
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

    # План 27.10 (graceful SIGTERM): обработчики и циклы в полёте доживают до
    # конца, ресурсы закрываются после них (app/core/shutdown.py).
    inflight = InFlight()
    dp.update.outer_middleware(InFlightMiddleware(inflight))

    # Отдельный SecretCipher, не тот, что в dp["cipher"]: фоновые задачи не
    # зависят от build_dispatcher, а сам шифр не хранит состояния — второй
    # экземпляр с тем же ключом безвреден.
    background_jobs = BackgroundJobs(
        bot, db, settings, SecretCipher(settings.encryption_key.get_secret_value()), redis,
        # 03.10: хранилище FSM того же процесса — «пользователь вводит число».
        input_gate=InputGate(dp.storage, bot.id),
        inflight=inflight,
    )
    # B.1: «Открыть» с тревогой — быстрые повторы защиты тем же воркером.
    dp["alarm_retry"] = background_jobs.openings.retry_alarm

    closers: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("background_jobs", background_jobs.shutdown),
    ]
    if llm_client is not None:
        closers.append(("llm", llm_client.aclose))
    closers += [("telegram", bot.session.close), ("redis", redis.aclose), ("db", db.dispose)]

    try:
        await recover_on_start(background_jobs, settings, stop)
        if not stop.is_set():
            background_jobs.start()
            await setup_bot_ui(bot, settings)
            # Сбрасываем накопившиеся за простой апдейты: обрабатывать команды
            # недельной давности бессмысленно и потенциально вредно.
            await bot.delete_webhook(drop_pending_updates=True)
        if not stop.is_set():
            await poll_until_stopped(dp, bot, stop, before_stop=background_jobs.pause)
    finally:
        logger.info("Остановка приложения")
        started = stop.at if stop.at is not None else time.monotonic()
        try:
            await shutdown(background_jobs, inflight, closers, stop=stop)
        finally:
            stop.uninstall()
        log_finished(started)


async def recover_on_start(
    background_jobs: BackgroundJobs, settings: Settings, stop: StopSignal
) -> None:
    """Открытия сделок из бота, прерванные рестартом (вход ушёл без ответа,
    исполнен без подтверждённого стопа), и действия этапа 4, прерванные
    посреди «Да» (B.9, только чтение биржи), — до поллинга и до первого цикла.
    Сигнал остановки между шагами — следующий шаг не начинается (его дело
    возьмёт следующий старт)."""
    if not settings.background_jobs_enabled:
        return
    if stop.is_set():
        return
    try:
        await background_jobs.openings.run()
    except Exception:
        logger.exception("Восстановление открытий при старте упало — повторит цикл")
    if stop.is_set():
        return
    try:
        await background_jobs.recover_actions()
    except Exception:
        logger.exception("Восстановление действий при старте упало — повторит цикл")


async def shutdown(
    background_jobs: BackgroundJobs,
    inflight: InFlight,
    closers: Sequence[tuple[str, Callable[[], Awaitable[None]]]],
    *,
    timeout: float = BOT_SHUTDOWN_DRAIN_SECONDS,
    stop: StopSignal | None = None,
) -> None:
    """Порядок: новых циклов нет → ожидание обработчиков, циклов и быстрых
    повторов тревоги общим сроком (повторный сигнал — отмена сразу) → отмена
    не уложившихся, пока Redis и БД открыты (их finally снимает локи) → только
    потом ресурсы. Ресурсы закрываются и при сбое паузы или ожидания."""
    try:
        background_jobs.pause()
        await drain(inflight, background_jobs.alarm_retries(), timeout=timeout, stop=stop)
    finally:
        await close_all(closers)


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Получен сигнал остановки")


if __name__ == "__main__":
    main()
