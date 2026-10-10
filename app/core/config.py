"""Конфигурация приложения.

Единственная точка чтения окружения. Все секреты — SecretStr, чтобы они
не попали в логи при случайном repr() объекта настроек.
"""

from __future__ import annotations

import math
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.analysis.ai.pricing import is_known, pricing_for
from app.trading.enums import ExchangeKeyMode

# 02.10.2026: путь входа по сигналу удалён (ExecutionService, handlers/
# execution.py). Расчёт ниже описывает его и пока остаётся источником TTL
# лока (confirm_lock_ttl_seconds, его видит reconciler); путь действий с
# позициями (этап 4) пересчитает его под себя.
#
# Раздел 8 ТЗ / раздел 16 ТЗ (шаг 15.5.1): число HTTP-вызовов на пути
# подтверждения («Да»), каждый max_retries=1 (не путать с картой карточки —
# там обычный ретрай клиента, см. ExecutionService.evaluate()). Список:
#   get_ticker          — app/exchanges/bingx.py (BingXClient.get_ticker)
#   get_balance          — app/exchanges/bingx.py (BingXClient.get_balance)
#   get_symbol_info      — app/exchanges/bingx.py (BingXClient.get_symbols)
#   get_leverage         — app/exchanges/bingx.py (BingXClient.get_leverage)
#   set_leverage         — app/exchanges/bingx.py (BingXClient.set_leverage)
#   place_market_order   — app/exchanges/bingx.py (BingXClient.place_market_order)
#   get_positions        — шаг 15.5.4а, гвард EXCHANGE_POSITION_EXISTS в
#                          _submit_real_order (ExecutionService.check_exchange_position)
# Последние три — часть шага 15.5.2 (сама отправка ещё не собрана в этом
# шаге), но уже спроектированы именно для confirm-пути (get_leverage/
# set_leverage — условная смена плеча перед входом, place_market_order —
# сам вход), поэтому считаем здесь заранее.
#
# get_position_mode НЕ в этом списке: читается только при построении
# карточки (app/services/position_mode.py), под локом на «Да» не
# перезапрашивается — см. handlers/execution.py:_build_quote,
# known_dual_side_position.
#
# Правило на будущее: константу обновляет тот шаг, который реально
# добавляет запрос на confirm-путь (или, как здесь — тот, где путь уже
# спроектирован достаточно точно, чтобы посчитать заранее без гадания) —
# не более ранний шаг "с запасом на всякий случай".
#
# Шаг 15.5.3: read-back входа идёт под тем же локом (решение владельца,
# вариант A). Худший путь добавляет (app/execution/readback.py):
#   get_order_fill       — 1 поиск при UNKNOWN + exec_order_readback_attempts
#                          чтений «дальше как SUBMITTED» (число — из
#                          Settings, см. confirm_path_http_calls)
#   get_open_orders      — 2: чтение + перечтение перед «стопа нет» (Р2)
#   place_conditional_order — 2: спасение стопа + спасение тейка
#
# Шаг 15.5.4а: пересчёт карточки после PRICE_DRIFT на «Да» тоже идёт под
# локом — ticker/symbol/balance дважды + get_positions карточки = 7 вызовов,
# не больше пути отправки; худшим остаётся путь отправки с read-back.
_ENTRY_PATH_HTTP_CALLS = 7
# 28.09 (блок D): + get_positions после условников — liquidationPrice против
# стопа, одна попытка без повторов.
_READBACK_FIXED_HTTP_CALLS = 1 + 2 + 2 + 1
# 28.09: худший сон троттлера BingXClient._maybe_throttle на пути «Да».
# Состояние лимита — по (метод, путь); сон возможен только на повторном
# вызове того же ключа, если после первого остаток <= порога 20
# (bingx_rate_limit_threshold). Окна приватных ручек — 1 с (сняты 28.09,
# CLAUDE.md), сон <= 1 с на каждый такой повтор:
#   GET  user/positions   — 2 вызова (гвард + ликвидация), лимит 10 → 1 сон
#   POST trade/order      — 3 вызова (вход + 2 спасения), лимит ~10 → 2 сна
#   GET  trade/openOrders — 2 вызова (чтение + перечтение), лимит 5 → 1 сон
#   GET  trade/order      — до 4 чтений, лимит 30, остаток 29 > 20 → 0
#   GET/POST trade/leverage, ticker, contracts, balance — по 1 → 0
# Пересчёт после дрейфа (ticker/contracts/balance ×2) не хуже: balance 40,
# публичные 500 — остаток выше порога.
_CONFIRM_PATH_THROTTLE_SLEEPS = 4
_PRIVATE_RATE_WINDOW_SECONDS = 1


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
    exec_confirm_ttl_seconds: int = 60
    # Запас поверх расчётного худшего случая пути подтверждения — см.
    # Settings.confirm_lock_ttl_seconds ниже. Покрывает локальную часть
    # (запись в БД, планировщик event loop), не сеть — сетевая часть уже
    # взята с запасом самой формулой (расчёт по timeout, не по типовой
    # длительности ответа).
    exec_confirm_lock_margin_seconds: int = 10
    # Шаг 15.5.3: read-back входа после отправки (app/execution/readback.py).
    # Чтений исполнения по clientOrderID — до attempts, пауза между ними
    # delay_ms. UNKNOWN — один поиск после паузы unknown_search_delay_ms.
    # «Стопа нет» — только после повторного чтения openOrders через
    # open_orders_recheck_delay_ms: вложенный стоп может появиться отдельным
    # ордером не сразу после исполнения маркета (реальная задержка снимается
    # на 15.5.5). Все паузы входят в TTL лока (confirm_lock_ttl_seconds).
    exec_order_readback_attempts: int = 3
    exec_order_readback_delay_ms: int = 500
    exec_unknown_search_delay_ms: int = 1000
    exec_open_orders_recheck_delay_ms: int = 500
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
    # Taker-комиссия BingX на ногу — вход маркетом и выход условником (оба
    # исполняются как taker). Биржевую ставку не читаем: живьём на демо
    # 0.05% (27.09, SOL/LINK вход и выход). Боевую сверить в префлайте 15.7.
    exec_taker_fee_rate: Decimal = Decimal("0.0005")
    # Этап 4: стоп и тейк не ближе этой доли (в %) от mark price — иначе
    # сработали бы сразу. Безубыток доступен, только когда цена ушла за него
    # хотя бы на эту долю.
    exec_min_stop_distance_percent: Decimal = Decimal("0.1")
    # Час по местному времени пользователя для сводки исполнения (раздел 12а).
    exec_daily_digest_hour: int = 21
    # Раздел 16 ТЗ, шаг 15.5.1: дефолт True — только DRY_RUN. False — реальная
    # отправка (15.5.2) с read-back (15.5.3), журналом (15.5.4) и гвардом
    # живой позиции (15.5.4а); старт-валидатор снят на 15.5.5. Боевой счёт
    # защищает не этот флаг, а exec_allow_live_mode_orders ниже.
    exec_dry_run: bool = True
    # Раздел 16 ТЗ, шаг 15.5.1: второй, более узкий выключатель поверх
    # trading_execution_enabled — реальные ордера на LIVE только по
    # явному включению, не как побочный эффект общего рубильника. См.
    # guards.check_live_orders_allowed.
    exec_allow_live_mode_orders: bool = False

    # --- Открытие сделки из бота (05.10.2026, docs/open-trade-plan.md) ------
    # Отдельные от EXEC_DRY_RUN выключатели: действия этапа 4 на демо живые,
    # а открытие сначала идёт сухим прогоном. Live открытия — только явным
    # EXEC_OPEN_ALLOW_LIVE поверх exec_allow_live_mode_orders.
    exec_open_dry_run: bool = True
    exec_open_allow_live: bool = False
    # Ликвидация дальше стопа минимум в столько раз (|вход − ликв.| ≥ k·|вход − стоп|).
    exec_open_liq_buffer: Decimal = Decimal("1.5")
    # Поддерживающая маржа в оценке ликвидации до входа — консервативно (на
    # Р3 факт ≈ 0.41%). После входа сверяется фактическая liquidationPrice.
    exec_open_mmr: Decimal = Decimal("0.01")
    # Срок лимитного входа по умолчанию и допустимые варианты, минуты.
    exec_open_limit_expiry_minutes: int = 240
    # Цена ушла с карточки: риск $ или объём изменились больше — отказ,
    # новая карточка.
    exec_open_card_drift_percent: Decimal = Decimal("10")
    # Срок карточки открытия (08.10.2026: 60 с мало, чтобы прочитать карточку на
    # телефоне). От ухода цены защищает exec_open_card_drift_percent. Истекла —
    # мастер сразу показывает пересчитанную карточку, «Открыть» — заново.
    exec_open_card_ttl_seconds: int = 120
    # «Стоп не подтверждён» (ALARM без закрытия): повторная тревога через столько
    # секунд, дальше напоминание с этим интервалом, пока не решится.
    exec_open_unconfirmed_alarm_seconds: int = 120
    exec_open_unconfirmed_remind_seconds: int = 300
    # B.1 (10.10.2026): быстрые повторы защиты после объявления ALARM — секунды
    # от тревоги, по возрастанию; дальше обычный цикл openings_interval_seconds.
    # Цель владельца — без стопа ≤ 8 с при живом боте (Т3 было 18.7 с).
    exec_open_alarm_retry_seconds: str = "1.5,3,5"
    # «🔴 Да, закрыть» под тревогой ждёт лок позиции до N с (деплой 3, фикс 5:
    # на 🔴 10.10 первое нажатие отказало — лок держал повтор защиты); повторы и
    # цикл на это время уступают (ключ close_wanted живёт N + 4 с). 8 — решение
    # владельца: ответ на нажатие должен успеть до таймаута Telegram.
    exec_open_close_lock_wait_seconds: float = Field(default=8.0, gt=0, le=12)
    # Управляемый сбой открытия для проверки аварийных веток на ДЕМО (08.10.2026,
    # по аналогии с --fault в rehearse_migration.sh). Список через запятую из
    # OPEN_FAULTS; каждый срабатывает только на первой попытке в открытии
    # (кроме fail_backup_stop_always — на всех автоматических).
    # По умолчанию пусто; в override прода — только на время проверки. При
    # любом признаке live (BINGX_TRADING_MODE=live, EXEC_OPEN_ALLOW_LIVE,
    # EXEC_ALLOW_LIVE_MODE_ORDERS) бот не стартует.
    exec_open_fault: str = ""

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
    # Этап 2: пересказ цифр «Анализа рынка» моделью. Выключен по умолчанию;
    # включается только вместе с ai_enabled. Модель не добавляет чисел (проверка
    # в app/analysis/market_summary.py), бюджет — общий ai_monthly_budget_usd.
    ai_market_summary_enabled: bool = False
    ai_market_summary_max_tokens: int = 300

    # --- Фоновые задачи (этап 12) --------------------------------------------
    background_jobs_enabled: bool = Field(
        default=False,
        description="Глобальный выключатель. Держать false на время доработок этапа.",
    )
    # Этап 5: монитор приближения к SL/TP. Mark price — каждый цикл (один
    # публичный запрос на символ), позиции и ордера с биржи — раз в
    # snapshot_seconds (2 приватных запроса на пользователя). Порог — на
    # пользователя (user_settings.sl/tp_alert_percent), глобального больше нет.
    position_monitor_price_seconds: int = 15
    position_monitor_snapshot_seconds: int = 60
    # Шаг 15.6: сверка журнала с биржей (раздел 10 ТЗ). Штатный цикл — один
    # запрос позиций; openOrders (есть ли стоп) — раз в N циклов.
    reconciler_interval_seconds: int = 60
    # 05.10.2026: восстановление незавершённых открытий сделок из бота и
    # (с шага 4) лимитные входы — истечение, исполнение, частичное.
    openings_interval_seconds: int = 15
    reconciler_stop_check_every: int = 5
    # 28.09: недоставленное уведомление сверки переотправляется до этого
    # возраста (расписание — app/execution/redelivery.py), дальше — отказ.
    reconciler_notify_max_age_hours: int = 24
    # 28.09: INFO-строка «Пульс reconciler» раз в столько запусков (циклы и
    # пропуски по локу). При интервале 60 с — раз в час, 24 строки в сутки.
    reconciler_pulse_every: int = 60
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

    @field_validator("exec_taker_fee_rate")
    @classmethod
    def _sane_fee_rate(cls, value: Decimal) -> Decimal:
        """Ставка — доля, не проценты: 0.0005, а не 0.05. Ошибка на порядок
        тихо съела бы весь RR или обнулила бы комиссию — роняем старт."""
        if not Decimal(0) <= value < Decimal("0.01"):
            raise ValueError(
                f"EXEC_TAKER_FEE_RATE должен быть в [0; 0.01) — доля, не проценты: {value}"
            )
        return value

    @field_validator("exec_open_fault")
    @classmethod
    def _known_open_faults(cls, value: str) -> str:
        unknown = {f for f in _split_faults(value) if f not in OPEN_FAULTS}
        if unknown:
            raise ValueError(
                f"EXEC_OPEN_FAULT: неизвестные сбои {sorted(unknown)}; "
                f"допустимы {sorted(OPEN_FAULTS)}"
            )
        return value

    @model_validator(mode="after")
    def _open_fault_only_on_demo(self) -> Settings:
        """Управляемый сбой — только демо: при любом признаке live отказ на старте."""
        if self.exec_open_faults and (
            self.bingx_trading_mode == "live"
            or self.exec_open_allow_live
            or self.exec_allow_live_mode_orders
        ):
            raise ValueError(
                "EXEC_OPEN_FAULT разрешён только на демо: снимите его или верните "
                "BINGX_TRADING_MODE=demo, EXEC_OPEN_ALLOW_LIVE=false, "
                "EXEC_ALLOW_LIVE_MODE_ORDERS=false"
            )
        return self

    @field_validator("exec_open_alarm_retry_seconds")
    @classmethod
    def _alarm_retry_offsets(cls, value: str) -> str:
        try:
            offsets = [float(x) for x in value.split(",") if x.strip()]
        except ValueError as exc:
            raise ValueError(f"EXEC_OPEN_ALARM_RETRY_SECONDS: не числа: {value!r}") from exc
        if any(x <= 0 for x in offsets) or offsets != sorted(set(offsets)):
            raise ValueError(
                f"EXEC_OPEN_ALARM_RETRY_SECONDS: положительные по возрастанию: {value!r}"
            )
        return value

    @property
    def exec_open_alarm_retry_offsets(self) -> tuple[float, ...]:
        return tuple(
            float(x) for x in self.exec_open_alarm_retry_seconds.split(",") if x.strip()
        )

    @property
    def exec_open_faults(self) -> frozenset[str]:
        return frozenset(_split_faults(self.exec_open_fault))

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
    def confirm_path_http_calls(self) -> int:
        """Худший случай числа HTTP-вызовов на пути «Да» вместе с read-back
        (шаг 15.5.3) — см. комментарий у _ENTRY_PATH_HTTP_CALLS. При
        дефолтах: 7 + (1 + 3) + 2 + 2 + 1 = 16 (последний — ликвидация, 28.09)."""
        return (
            _ENTRY_PATH_HTTP_CALLS
            + self.exec_order_readback_attempts
            + _READBACK_FIXED_HTTP_CALLS
        )

    @property
    def confirm_path_sleep_seconds(self) -> float:
        """Паузы read-back в худшем случае (шаг 15.5.3): поиск при UNKNOWN,
        паузы между чтениями исполнения, перечтение openOrders. При
        дефолтах: 1.0 + 2 × 0.5 + 0.5 = 2.5 с."""
        return (
            self.exec_unknown_search_delay_ms
            + max(self.exec_order_readback_attempts - 1, 0) * self.exec_order_readback_delay_ms
            + self.exec_open_orders_recheck_delay_ms
        ) / 1000

    @property
    def confirm_path_throttle_seconds(self) -> int:
        """Худший сон троттлера на пути «Да» (28.09): 4 повтора ключа с
        остатком ниже порога × окно приватной ручки 1 с = 4 с. Разбор — у
        _CONFIRM_PATH_THROTTLE_SLEEPS."""
        return _CONFIRM_PATH_THROTTLE_SLEEPS * _PRIVATE_RATE_WINDOW_SECONDS

    @property
    def confirm_lock_ttl_seconds(self) -> int:
        """TTL Redis-лока подтверждения (раздел 8 ТЗ / раздел 16 ТЗ, шаг
        15.5.1), выведенный из реальных таймаутов, а не литерал.

        ttl = ceil(http_timeout_seconds × (confirm_path_http_calls + 1))
              + exec_confirm_lock_margin_seconds
              + ceil(confirm_path_sleep_seconds)
              + confirm_path_throttle_seconds

        "+1" внутри множителя — явный запас сверх посчитанного числа
        запросов (раздел 16 ТЗ), отдельно от exec_confirm_lock_margin_seconds
        (тот покрывает локальную часть — БД, планировщик event loop, не
        сеть). Шаг 15.5.3: read-back под тем же локом — его вызовы входят в
        confirm_path_http_calls, паузы — отдельным членом. 28.09: сон
        троттлера — ещё одним членом. При дефолтах:
        ceil(10.0 × (16+1)) + 10 + ceil(2.5) + 4 = 170 + 10 + 3 + 4 = 187.

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
            math.ceil(self.http_timeout_seconds * (self.confirm_path_http_calls + 1))
            + self.exec_confirm_lock_margin_seconds
            + math.ceil(self.confirm_path_sleep_seconds)
            + self.confirm_path_throttle_seconds
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


OPEN_FAULTS = frozenset({
    "skip_attached_stop",     # вход уходит без вложенного стопа (тейк остаётся)
    "fail_backup_stop",       # запасной стоп не отправляется — «отклонён»
    "fail_emergency_close",   # аварийное закрытие не отправляется — «отклонено»
    "hide_backup_stop",       # запасной стоп отправлен, но скрыт в openOrders и по cid
    # A.1 (10.10.2026): ALARM держится, пока владелец не нажмёт «🔴 Закрыть
    # маркетом» — КАЖДАЯ попытка запасного стопа и КАЖДОЕ автоматическое
    # аварийное закрытие «отклонены»; ручное закрытие кнопкой — нет. На демо —
    # вместе со skip_attached_stop (иначе позицию прикроет вложенный стоп).
    "fail_backup_stop_always",
})


def _split_faults(value: str) -> list[str]:
    return [f.strip() for f in value.split(",") if f.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный синглтон настроек.

    lru_cache вместо модульной переменной: не читает окружение на импорте,
    что позволяет подменять настройки в тестах через get_settings.cache_clear().
    """
    return Settings()  # type: ignore[call-arg]
