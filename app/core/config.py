"""Конфигурация приложения.

Единственная точка чтения окружения. Все секреты — SecretStr, чтобы они
не попали в логи при случайном repr() объекта настроек.
"""

from __future__ import annotations

import math
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.analysis.ai.pricing import is_known, pricing_for
from app.trading.enums import ExchangeKeyMode

# Раздел 8 ТЗ: число HTTP-вызовов на пути подтверждения («Да»), каждый
# max_retries=1 (не путать с картой карточки — там обычный ретрай клиента,
# см. ExecutionService.evaluate()). Меняется этот список — меняй и число:
#   get_ticker       — app/exchanges/bingx.py (BingXClient.get_ticker)
#   get_balance      — app/exchanges/bingx.py (BingXClient.get_balance)
#   get_symbol_info  — app/exchanges/bingx.py (BingXClient.get_symbols)
#   set_leverage     — app/exchanges/bingx.py (BingXClient.set_leverage)
#   place_market_order — app/exchanges/bingx.py (BingXClient.place_market_order)
# Последние два — часть этапа 15.5 (ещё не отправляют реальный ордер), но
# TTL лока обязан учитывать их заранее, иначе 15.5 добавит вызовы, а лок
# останется рассчитан на путь без них.
_CONFIRM_PATH_HTTP_CALLS = 5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Окружение ---------------------------------------------------------
    environment: Literal["dev", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = Field(
        default=True,
        description="JSON-логи для прода, человекочитаемые для локальной разработки.",
    )

    # --- Telegram ----------------------------------------------------------
    bot_token: SecretStr
    allowed_telegram_ids: str = Field(
        description="Whitelist ID через запятую. Пустая строка = доступ открыт всем.",
        default="",
    )
    default_timezone: str = "Asia/Yekaterinburg"

    # --- База данных -------------------------------------------------------
    database_url: SecretStr
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 5

    # --- Шифрование биржевых ключей ---------------------------------------
    encryption_key: SecretStr = Field(
        description="Fernet-ключ. Сгенерировать: python -m scripts.generate_key",
    )

    # --- Прокси для Telegram ----------------------------------------------
    # Нужен, если хостинг блокирует IP Telegram (типично для российских
    # дата-центров с DPI). Формат: socks5://user:pass@host:port
    # или http://user:pass@host:port. Пустая строка = прямое подключение.
    telegram_proxy: SecretStr | None = None

    # --- BingX -------------------------------------------------------------
    bingx_base_url: str = "https://open-api.bingx.com"
    # Хосты по режиму ключа (этап 15.4в) — куда ExchangeFactory ходит за
    # балансом/позициями для показа, в зависимости от того, ключ какого
    # режима используется. Демо-хост проверен по живому API (не по статье,
    # раздел 16 ТЗ): GET .../openApi/swap/v2/quote/contracts на
    # open-api-vst.bingx.com отвечает 200 с реальными контрактами.
    bingx_live_base_url: str = "https://open-api.bingx.com"
    bingx_demo_base_url: str = "https://open-api-vst.bingx.com"
    # Куда РЕАЛЬНО уходят ордера (этап 15.5+) — не путать с переключателем
    # показа в настройках (UserSettings.active_exchange_mode). Меняется
    # только через .env + рестарт, кнопки в боте нет (раздел 3 ТЗ).
    bingx_trading_mode: Literal["demo", "live"] = "demo"
    bingx_recv_window: int = 5000
    http_timeout_seconds: float = 10.0
    http_max_retries: int = 3
    # Троттлинг сканера (см. app/exchanges/bingx.py, раздел
    # X-RateLimit-Requests-Remain/-Expire): при remaining <= порога клиент
    # сам ждёт до конца окна ПЕРЕД следующим запросом, не дожидаясь 429.
    # Лимит у BingX отдельный на каждый путь и сильно разного масштаба
    # (публичные ~500/10с, приватные вроде trade/openOrders — всего 5/1с) —
    # один порог не идеален для всех, но раздел ТЗ просит простой порог,
    # не token bucket. Выключатель — на случай, если порог окажется вреден
    # для каких-то приватных ручек и понадобится быстро откатить без деплоя.
    bingx_rate_limit_threshold: int = 20
    bingx_rate_limit_throttle_enabled: bool = True

    # --- Redis (этап 15: блокировка от двойного нажатия «Да», раздел 8) ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Исполнение сделок по подтверждению (этап 15, раздел 11 ТЗ) -------
    trading_execution_enabled: bool = Field(
        default=False,
        description=(
            "Главный выключатель модуля execution. По умолчанию выключен; "
            "переключатель в настройках бота может только выключить его "
            "дополнительно, но не включить при False здесь."
        ),
    )
    exec_max_open_positions: int = 4
    exec_max_total_risk_percent: Decimal = Decimal("5")
    # Доля расстояния до стопа, в пределах которой дрейф цены на подтверждении
    # ещё не требует пересчёта карточки (раздел 5, 9 ТЗ).
    exec_max_price_drift_ratio: Decimal = Decimal("0.3")
    # Пакет B: доля расстояния от опорной цены сигнала (середина entry-зоны)
    # до его же стопа — насколько цена может уйти в сторону тейка с момента
    # сигнала, прежде чем гвард SIGNAL_STALE откажет. В отличие от
    # exec_max_price_drift_ratio, который сравнивает с ценой карточки, этот
    # сравнивает с ценой сигнала и работает уже на первом показе.
    exec_max_signal_staleness_ratio: Decimal = Decimal("1.0")
    exec_confirm_ttl_seconds: int = 60
    # Запас поверх расчётного худшего случая пути подтверждения — см.
    # Settings.confirm_lock_ttl_seconds ниже. Покрывает локальную часть
    # (запись в БД, планировщик event loop), не сеть — сетевая часть уже
    # взята с запасом самой формулой (расчёт по timeout, не по типовой
    # длительности ответа).
    exec_confirm_lock_margin_seconds: int = 10
    # Раздел 8 ТЗ: как часто перепроверять права ключа (GET .../apiRestrictions)
    # при построении карточки подтверждения — см. app/services/permissions.py.
    exec_permissions_ttl_hours: int = 6
    # Раздел 16 ТЗ, шаг 15.5.1: как часто перепроверять режим позиций
    # (GET .../positionSide/dual) при построении карточки — см.
    # app/services/position_mode.py. Короче, чем exec_permissions_ttl_hours:
    # режим позиций меняется на стороне биржи не так инертно, как права
    # ключа, и промах кэша здесь дешевле (один лишний GET, не поход за
    # apiRestrictions с более тяжёлыми последствиями отказа).
    exec_position_mode_ttl_seconds: int = 300
    exec_min_rr: Decimal = Decimal("1.5")
    exec_symbol_whitelist: str = Field(
        default="BTC-USDT,ETH-USDT",
        description="Через запятую. Пустая строка = ограничения нет.",
    )
    # Час по местному времени пользователя для сводки исполнения (раздел 12а).
    exec_daily_digest_hour: int = 21
    # Раздел 16 ТЗ, шаг 15.5.1: дефолт True сохраняет сегодняшнее поведение
    # (только DRY_RUN) без изменений. False пока не поддерживается — см.
    # валидатор _dry_run_supported_only_when_true ниже: путь реальной
    # отправки (PENDING-запись до HTTP, place_market_order) появится
    # только в 15.5.2, а не в этом шаге.
    exec_dry_run: bool = True
    # Раздел 16 ТЗ, шаг 15.5.1: второй, более узкий выключатель поверх
    # trading_execution_enabled — реальные ордера на LIVE только по
    # явному включению, не как побочный эффект общего рубильника. См.
    # guards.check_live_orders_allowed.
    exec_allow_live_mode_orders: bool = False

    # --- Дефолты торгового плана ------------------------------------------
    # Реальные значения хранятся в БД per-user; это лишь начальные значения
    # при создании плана, а не источник истины во время работы.
    default_risk_per_trade_percent: Decimal = Decimal("2.0")
    default_max_daily_loss_percent: Decimal = Decimal("6.0")
    default_max_weekly_loss_percent: Decimal = Decimal("10.0")
    default_max_trades_per_day: int = 5
    min_risk_reward: Decimal = Decimal("2.0")

    # --- Импорт истории ----------------------------------------------------
    import_history_days: int = 365
    import_poll_interval_seconds: int = 300

    # --- AI-слой (этап 11) --------------------------------------------------
    ai_enabled: bool = True
    anthropic_api_key: SecretStr | None = None
    ai_pricing_profile: str = "anthropic"
    ai_model: str = "claude-sonnet-5"
    ai_max_output_tokens: int = 1500
    ai_max_input_tokens: int = 20000
    ai_monthly_budget_usd: Decimal = Decimal("5")
    ai_base_url: str = "https://api.anthropic.com"
    ai_auth_scheme: str = "x-api-key"

    # --- Фоновые задачи (этап 12) --------------------------------------------
    background_jobs_enabled: bool = Field(
        default=False,
        description="Глобальный выключатель. Держать false на время доработок этапа.",
    )
    setup_scanner_interval_minutes: int = 30
    # Он же порог "прошло N часов" для повторного уведомления по тому же
    # сетапу — то же самое TTL, а не два независимых числа (см. app/workers/scanner.py).
    setup_scanner_ttl_hours: int = 4
    position_monitor_interval_minutes: int = 5
    # Доля оставшегося пути от входа до TP/SL, при которой считаем, что цена
    # "приблизилась" — 10 означает последние 10% дистанции.
    position_monitor_approach_percent: Decimal = Decimal("10")
    daily_jobs_interval_minutes: int = 15
    daily_summary_hour_local: int = 20

    @field_validator("exec_dry_run")
    @classmethod
    def _dry_run_supported_only_when_true(cls, value: bool) -> bool:
        """Раздел 16 ТЗ, шаг 15.5.1: узнавать о недостроенном пути реальной
        отправки в момент нажатия «Да» — с уже взятым Redis-локом и уже
        помеченным signal.trade_opened_at — недопустимо. EXEC_DRY_RUN=false
        роняет запуск процесса, а не ждёт первого подтверждения. Валидацию
        снимает шаг 15.5.2, когда путь реальной отправки появится."""
        if not value:
            raise ValueError(
                "EXEC_DRY_RUN=false не поддерживается до шага 15.5.2 — "
                "путь реальной отправки ордера ещё не реализован."
            )
        return value

    @field_validator("ai_model")
    @classmethod
    def _known_model(cls, value: str, info) -> str:
        """Опечатка в AI_MODEL роняет старт, а не учёт расходов.

        Фолбэка по цене нет: неизвестная модель означала бы либо нулевой
        расход (лимит не сработает), либо цену наугад.
        """
        profile = info.data.get("ai_pricing_profile", "anthropic")
        if not is_known(value, profile):
            known = ", ".join(sorted(pricing_for(profile)))
            raise ValueError(f"unknown AI_MODEL: {value} in profile {profile}. Known: {known}")
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: SecretStr) -> SecretStr:
        """SQLAlchemy async-движок молча падает на синхронном драйвере."""
        url = value.get_secret_value()
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL должен начинаться с postgresql+asyncpg:// "
                "(асинхронный драйвер обязателен)"
            )
        return value

    @field_validator("encryption_key")
    @classmethod
    def _validate_fernet_key(cls, value: SecretStr) -> SecretStr:
        from cryptography.fernet import Fernet

        try:
            Fernet(value.get_secret_value().encode())
        except Exception as exc:
            raise ValueError(
                "ENCRYPTION_KEY невалиден. Сгенерируй: python -m scripts.generate_key"
            ) from exc
        return value

    @property
    def confirm_lock_ttl_seconds(self) -> int:
        """TTL Redis-лока подтверждения (раздел 8 ТЗ), выведенный из реальных
        таймаутов, а не литерал.

        ttl = ceil(http_timeout_seconds × _CONFIRM_PATH_HTTP_CALLS)
              + exec_confirm_lock_margin_seconds

        При max_retries=1 на каждом из этих вызовов (см. п.1-2 разведки)
        бэкофф между попытками не наступает — цикл в BingXClient._request
        не спит перед последней попыткой, поэтому в сумме нет ничего, кроме
        самих таймаутов.

        Это ОЦЕНКА, не гарантия: httpx.AsyncClient(timeout=X) применяет X
        отдельно к фазам connect и read (и к write/pool) одного HTTP-вызова,
        а не как общий потолок на весь вызов — один запрос в патологическом
        случае (например, connect почти прошёл, потом завис read) способен
        занять заметно больше http_timeout_seconds. TTL не пытается это
        поймать явным множителем: он существует только как предохранитель
        на случай, если процесс умрёт до RedisLock.__aexit__ (см. app/core/
        locks.py) — единственная настоящая гарантия единственности входа
        это UNIQUE на execution_orders.client_order_id (раздел 8 ТЗ), не
        этот TTL и не сам факт удержания лока.
        """
        return (
            math.ceil(self.http_timeout_seconds * _CONFIRM_PATH_HTTP_CALLS)
            + self.exec_confirm_lock_margin_seconds
        )

    @property
    def allowed_ids(self) -> frozenset[int]:
        """Разобранный whitelist. Пустое множество = ограничения нет."""
        raw = self.allowed_telegram_ids.strip()
        if not raw:
            return frozenset()
        return frozenset(int(part) for part in raw.split(",") if part.strip())

    @property
    def bingx_allowed_exchange_mode(self) -> ExchangeKeyMode:
        """Режим, разрешённый конфигом для реальной отправки (раздел 3, 11
        ТЗ) — guards.check_mode_allowed (этап 15.4в) сверяет с ним то, что
        выбрано в настройках пользователя, и отказывает при расхождении."""
        return ExchangeKeyMode.LIVE if self.bingx_trading_mode == "live" else ExchangeKeyMode.DEMO

    @property
    def exec_symbol_whitelist_symbols(self) -> tuple[str, ...]:
        """Разобранный список инструментов, разрешённых к исполнению.

        Пустой кортеж = ограничения нет (см. guards.check_symbol_allowed).
        """
        raw = self.exec_symbol_whitelist.strip()
        if not raw:
            return ()
        return tuple(part.strip().upper() for part in raw.split(",") if part.strip())

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Строки, которые фильтр логирования обязан вырезать из вывода."""
        values = [
            self.bot_token.get_secret_value(),
            self.database_url.get_secret_value(),
            self.encryption_key.get_secret_value(),
        ]
        # URL прокси содержит логин и пароль — в лог он попасть не должен.
        if self.telegram_proxy is not None:
            values.append(self.telegram_proxy.get_secret_value())
        # Ключ AI-провайдера не должен попасть в лог даже через traceback httpx.
        if self.anthropic_api_key is not None:
            values.append(self.anthropic_api_key.get_secret_value())
        return tuple(values)

    @property
    def proxy_url(self) -> str | None:
        if self.telegram_proxy is None:
            return None
        value = self.telegram_proxy.get_secret_value().strip()
        return value or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный синглтон настроек.

    lru_cache вместо модульной переменной: не читает окружение на импорте,
    что позволяет подменять настройки в тестах через get_settings.cache_clear().
    """
    return Settings()  # type: ignore[call-arg]
