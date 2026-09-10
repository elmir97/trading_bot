"""Конфигурация приложения.

Единственная точка чтения окружения. Все секреты — SecretStr, чтобы они
не попали в логи при случайном repr() объекта настроек.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.analysis.ai.pricing import is_known, pricing_for


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
    # demo — торговля виртуальными USDT (VST) на демо-хосте (этап 15.5);
    # live переключается вручную через .env + рестарт, кнопки в боте нет.
    bingx_trading_mode: Literal["demo", "live"] = "demo"
    bingx_recv_window: int = 5000
    http_timeout_seconds: float = 10.0
    http_max_retries: int = 3

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
    exec_confirm_ttl_seconds: int = 60
    exec_min_rr: Decimal = Decimal("1.5")
    exec_symbol_whitelist: str = Field(
        default="BTC-USDT,ETH-USDT",
        description="Через запятую. Пустая строка = ограничения нет.",
    )
    # Час по местному времени пользователя для сводки исполнения (раздел 12а).
    exec_daily_digest_hour: int = 21

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
        except Exception as exc:  # noqa: BLE001 - хотим понятное сообщение
            raise ValueError(
                "ENCRYPTION_KEY невалиден. Сгенерируй: python -m scripts.generate_key"
            ) from exc
        return value

    @property
    def allowed_ids(self) -> frozenset[int]:
        """Разобранный whitelist. Пустое множество = ограничения нет."""
        raw = self.allowed_telegram_ids.strip()
        if not raw:
            return frozenset()
        return frozenset(int(part) for part in raw.split(",") if part.strip())

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
