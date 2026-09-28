# Trading Journal Bot — состояние проекта

Обновлено: 2026-09-22, вечер. Архитектура и решения — на 22.09. Текущее состояние
прода и очередь — только в `docs/handoff.md` (текущий раздел — сверху); раздел «Где
остановились» ниже устарел. Здесь — архитектура, решения, проверенные факты и история.

## Что это

Личный Telegram-бот для журнала сделок, аналитики и поиска сетапов на фьючерсах BingX.
Один пользователь. Монетизация — потом.

Операционные заметки (локальный прогон, деплой, конвенции) — в `CLAUDE.md` в корне
репозитория, его читает Claude Code. ТЗ этапа 15 — `docs/execution-stage-15.md`
в репозитории, источник истины по шагам.

## Где остановились

Прод на `c472018`. Голова миграций `c8e2d51a9f04`. Тестов 787, smoke 66/66.

Исполнение — сухой прогон, теперь флагом `EXEC_DRY_RUN=true` (зашитый `DRY_RUN`
из хендлера убран). Сделаны 15.5.1 (гейты, плечо, режим позиций, TTL лока) и
15.5.1а (логирование). Следующее — 15.5.2, отправка ордера за флагом.

Цепочка миграций: `ddace081d9fb` → `f3a92c7e1b0d` → `b4c1e9a7d203` → `4f3837b79361`
→ `c8e2d51a9f04`.

### Смена бота — решено, отложено до окна между 15.5 и 15.7

Владелец хочет другой публичный адрес. **Username бота в Telegram не меняется** — он
задаётся один раз при создании; в BotFather есть только Edit Name (отображаемое имя),
ссылка `t.me/<username>` от него не зависит. Значит нужен новый бот.

1. BotFather `/newbot`, задать желаемый username — он и станет ссылкой
2. Получить новый токен
3. На сервере заменить `BOT_TOKEN` в `.env`, **предварительно скопировав файл**
4. Перезапустить контейнер
5. **Написать новому боту `/start`** — до этого он не может писать первым, и фоновые
   уведомления будут упираться в ошибку

**Данные не теряются**: всё привязано к `telegram_id` пользователя, а не к боту.
Теряется только история переписки. Отдельный шаг с правкой `.env` на проде.

### Временное расширение `EXEC_SYMBOL_WHITELIST`

На период сухого прогона в `docker-compose.override.yml` на сервере прописан полный
список плана: BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, GRAMTON (все `-USDT`).
**Перед 15.7 сузить до одного символа** — так решено для первых боевых входов.

## Инфраструктура

- VPS Timeweb Cloud, Франкфурт, `root@147.45.111.10`, проект в `/opt/trading_bot`
- Docker Compose: `bot`, `postgres`, `redis` (+ `postgres_test` под профилем `test`)
- `docker-compose.override.yml` — **server-only, в git не отслеживается**. Содержит
  `TRADING_EXECUTION_ENABLED: "true"` и временный `EXEC_SYMBOL_WHITELIST`.
  `EXEC_DRY_RUN` и `EXEC_ALLOW_LIVE_MODE_ORDERS` там **не заданы** — работают дефолты
- **Резервный репозиторий**: приватный `github.com/elmir97/trading_bot`, remote `origin`.
  Не деплойный канал — деплой через `git archive`. После каждого коммита push
- Бэкапы БД: `/opt/backups/db/`, cron 03:07 UTC. Пред-деплойные снапшоты:
  `/opt/backups/trading_bot_pre_deploy_<ts>.sql.gz` и `…code_pre_deploy_<ts>.tar.gz`
- Логи: `/opt/trading_bot/logs/bot.log` смонтирован в хост, ротации нет
  (1.69 МБ с 06.09). На проде `LOG_JSON=false`
- В логе старта видно `environment=dev` — что читает эту настройку, не выяснено
- Бот `@elmir_trading_journal_bot`, id 8690367328 (планируется замена)
- Локально: `C:\Users\vladz\OneDrive\Desktop\trading\trading_bot`
- Часовой пояс пользователя `Asia/Yekaterinburg`, сервер в UTC

### Redis

Сервис `redis`, образ `redis:7.4-alpine`, `restart: unless-stopped`:

```
redis-server --appendonly no --save "" --maxmemory 256mb --maxmemory-policy noeviction
```

- **Порт наружу не публикуется, volume нет.** Проверено: `ss -tulpn | grep 6379` пусто
- **`noeviction`, не `allkeys-lru`.** Redis держит лок идемпотентности
  `exec:lock:{user_id}:{signal_id}`. Вытеснение по LRU = лок исчез = два ордера по одному
  сигналу. При `noeviction` переполнение даёт ошибку записи, лок не берётся, кнопка
  отказывает — безопасный исход
- **Персистентности нет.** Локи с TTL, дедуп сигналов (TTL 4 часа), кэш
- `bot` зависит через `depends_on: condition: service_healthy`, healthcheck `redis-cli ping`
- `REDIS_URL: redis://redis:6379/0` в `environment` сервиса `bot`

Проверка на проде: `docker compose exec bot python -m scripts.check_redis`.

### Логирование (`c472018`)

- `SecretRedactingFilter` висит **на хендлерах** stdout и file (оба на root). Фильтр на
  логгере не видел бы записей дочерних логгеров — так делать нельзя
- `TextFormatter` дописывает `extra={...}` как `key=value`. До `c472018` текстовый режим
  молча выбрасывал `extra` у всех строк
- `_RESERVED_LOG_RECORD_KEYS` — общий список стандартных полей `LogRecord` для фильтра
  и обоих форматтеров
- `httpx`, `httpcore` — WARNING: URL с подписью не создаёт `LogRecord`
- Скан логов прода 22.09 по `signature=`, `X-BX-APIKEY`, `apiKey`, `secret` — 0

## BingX — что проверено живыми запросами

Всё снято фактическими вызовами, не из документации.

### Демо-контур

- Хост `https://open-api-vst.bingx.com`
- **Ключ единый.** Боевой ключ аутентифицируется на демо-хосте
- Демо и бой — одна учётка BingX, права ключа общие. Демо это другой routing
- Актив демо-счёта — **`VST`**, не `USDT`. Баланс 22.09: 89 002.08 VST
- Боевой фьючерсный счёт: 0 USDT

### Подпись (`29a2c26`)

Фикс `signature mismatch` задеплоен 22.09. Живьём проверена подпись GET
(`get_balance()`, `code=0`). **Подпись POST с `takeProfit`/`stopLoss` живьём не
проверена** — проверит первый демо-ордер (15.5.5).

### Режим позиций — `GET /openApi/swap/v1/positionSide/dual` (22.09)

- **v1, не v2**: `/openApi/swap/v2/trade/positionSide/dual` отвечает `code 100404`
- Значение в `data.dualSidePosition`, не на верхнем уровне
- **Аккаунт в хедж-режиме** (`true`) → `positionSide` = `LONG`/`SHORT`
- Снято с демо-роутинга. **Перед 15.7 перечитать на боевом хосте**
- Реализация: `get_position_mode()`, `app/services/position_mode.py`, приватный
  in-memory `TTLCache` (300 с), неудача не кэшируется. Читается на построении карточки

### Плечо — `GET /openApi/swap/v2/trade/leverage?symbol=` (22.09)

- Ключи: `longLeverage`, `shortLeverage`, `maxLongLeverage`, `maxShortLeverage`,
  `availableLongVal/Vol`, `availableShortVal/Vol`, `maxPositionLongVal/ShortVal`, `symbol`
- BTC-USDT: текущее 20/20, максимум 150/150
- Реализация: `get_leverage()` → `LeverageInfo`, без дефолтов. `leverage_needs_update()`
  в `app/execution/leverage.py` сравнивает **по нужной стороне**. POST только при
  расхождении

### Контракты — `/openApi/swap/v2/quote/contracts`

- `pricePrecision`, `quantityPrecision`, `tradeMinQuantity`, `tradeMinUSDT` — есть
- **`maxLongLeverage`/`maxShortLeverage` нет.** `SymbolInfo.max_leverage` всегда был
  дефолтом 20 — удалён (`caabaa5`). Максимум плеча — из `/trade/leverage`
- Поле `size` (шаг лота) в коде не читается
- Точности BTC 11.09: `pricePrecision=1`, `quantityPrecision=4`, `tradeMinQuantity=0.0001`,
  `tradeMinUSDT=2`

### История ордеров — `GET /openApi/swap/v2/trade/allOrders`

- Окно запроса **≤ 7 дней** (`code 109400` при 30). Reconciler режет на отрезки
- Исполненных ордеров на демо за 7 дней нет → **имена полей факта исполнения
  (`avgPrice`, `executedQty`, `commission`) живьём не сняты** — снимет 15.5.5

### Rate limits — измерено, троттлинг реализован (`005f55f`)

`GET /openApi/swap/v3/quote/klines`, демо-хост: `x-ratelimit-requests-remain: 499`,
`x-ratelimit-requests-expire: 10000`. Окно 10 секунд, порядка 500 запросов.

- `_RateLimitState` **по пути**, не общий на клиент
- `_maybe_throttle(path)` перед запросом, `_update_rate_limit()` после ответа
- Отсутствие заголовков не роняет запрос и не трактуется как «остаток ноль»
- `bingx_rate_limit_threshold` (20), `bingx_rate_limit_throttle_enabled` (`True`)

`SetupScanner` раньше не сохранял клиент — тот терялся в замыкании `MarketDataService`,
троттлинг был бы декорацией. Поймано на этапе плана.

`ScanCycleStats` пишется в лог и строкой в сводку.

### Общие ключи как свойство биржи (`4bb2ef9`)

- `ExchangeClient.shares_keys_across_modes`, у `BingXClient` — `True`
- `ExchangeFactory.get_credentials()`: точный поиск по режиму, при пустом результате
  и включённом флаге — по другому. `mode` строки — метка «где ввели», `base_url` —
  по **запрошенному** режиму

### Выбор актива баланса (`13d3c7a`)

`_QUOTE_ASSET_BY_MODE = {LIVE: "USDT", DEMO: "VST"}`, при отсутствии актива —
`ExchangeResponseError`. Раньше молча брался первый элемент списка.

### Права ключа — `/openApi/v1/account/apiRestrictions` (`2f09fd9`)

- **Поля на ВЕРХНЕМ уровне JSON. Ключей `code`/`msg` в ответе нет вообще** —
  успех только по HTTP 200 (подтверждено 22.09)
- 22.09: `enableFutures=True`, `permitsUniversalTransfer=False`, `ipRestrict=True`
- **Не использовать `/openApi/v1/account/apiPermissions`** — права числами, `apiKey` эхом
- `refresh_permissions(..., ttl_hours, force)`, `EXEC_PERMISSIONS_TTL_HOURS=6`. Сбой
  при протухшей отметке → `PERMISSIONS_UNKNOWN`

### Выставленные ордера — `/openApi/swap/v2/trade/openOrders` (`8477d69`)

`data.orders` — список, поля ордера на верхнем уровне, вложенные `takeProfit`/`stopLoss`
(`type`, `quantity`, `stopPrice`, `price`, `workingType`, `stopGuaranteed`).

**`price` и `quantity` внутри `takeProfit`/`stopLoss` бывают нулями при реально
прикреплённом условнике. Признак «TP/SL задан» — `stopPrice != 0`.** Это же — признак
для read-back в 15.5.3.

Разделение «свои» (`clientOrderID` с префиксом `tj`) и «ручные» — в хендлере, не в клиенте.

### Как писать разведскрипты

Одноразовый скрипт **целиком инлайн** (`docker compose exec -T bot python - <<'PY'`),
на диск ничего. Credentials через `ExchangeFactory`. Ключи в чат не передавать. Для
подписанных ручек печатать `sorted(keys)` и перечисленные поля, не `repr()`.

**Разведка ничего не отправляет на биржу.** Выставление ордера, даже на демо — отдельное
действие с отдельным «да».

## Скрипты проверки

### `scripts/smoke_check.py`

66 проверок. Единственный харнесс для хендлеров `settings.py`. Гварды на старте: имя БД
(только `trading_bot_test`) и `TRADING_EXECUTION_ENABLED`. Самоочистка, все сущности на
`telegram_id=424242` (зарезервирован). Проверка — два прогона подряд.

Фейки `BingXClient` на execution-пути (`get_ticker`, `get_symbols`, `get_balance`,
`get_position_mode`) подставляются monkeypatch на класс и обязаны повторять текущие
сигнатуры, включая `max_retries`. 22.09 три поломки фейков шли каскадом (`2d9a4d8`).
**Открытый вопрос:** падала ли проверка [14] или проходила с неверным исходом.

## Форматирование чисел

Сейчас (28.09) в `app/core/numfmt.py` — `fmt_num` и `fmt_price` (плюс
`_fallback_price_precision`); `fmt_qty`, `fmt_ratio`, `fmt_money`, `fmt_percent`,
`fmt_amount` определены в `app/bot/formatting.py`, который реэкспортирует `numfmt`. Слой
`execution` не тянет `app.bot`. Решено: при переводе уведомлений reconciler на форматтеры
перенести все `fmt_*` в `app/core/numfmt.py` (`formatting.py` — только реэкспорт),
воркеры импортируют из `app.core`.

- **Все `quantize()` с явным `rounding=ROUND_HALF_UP`**
- Без точности: ≥1000 → 2 знака, ≥1 → 4, <1 → 6
- Точность от биржи через `get_symbol_info()`; сканер берёт список инструментов одним
  вызовом до цикла

Расчёты на полной точности Decimal, округление только на выводе.

## Наблюдение — сводка исполнения

Воронка, скользящие 24 часа, отправка в `EXEC_DAILY_DIGEST_HOUR`:

```
Сигналов READY: N
  показана карточка: M
    подтверждено / отказ пользователя / истекло по TTL
  отказ кода до карточки: K
Скан рынка: N символов, M запросов, T с
```

- `execution_orders.stage` (`card`/`confirm`), `NULL` = до карточки (`c8e2d51a9f04`)
- `OrderStatus.ERROR` — сбой биржи на пути входа, `error_message` пуст (в `str(exc)`
  может быть тело ответа биржи)
- Пороги выборки: `GUARD_DOMINANCE_MIN_ATTEMPTS = 5`, `PRICE_DRIFT_MIN_CARDS = 3`
- `day_bounds()` не трогать — три потребителя с календарной семантикой

С 15.7 — отдельная строка по каждому из первых боевых входов: проскальзывание,
комиссия, `executedQty`, SL/TP.

## Стек

Python 3.12, aiogram 3.x, SQLAlchemy 2.x (async, asyncpg), Alembic, pydantic-settings,
Redis, cryptography (Fernet), mplfinance, pytest, fakeredis[lua], Docker Compose.

## Готово: этапы 1–12

Каркас, БД, пользователи, журнал сделок, статистика, риск-менеджмент, клиент BingX
и импорт истории, Market Data с кэшем, индикаторы, Signal Engine (BreakoutRetest,
EMAPullback, вердикт LONG/SHORT/WAIT), AI-разбор, фоновые задачи и уведомления.

- **AI-разбор**: LLM ничего не считает, код собирает `FactPack` и `Finding`. ProxyAPI,
  кэш по sha256, месячный лимит, минимум 20 сделок, при сбое — детерминированный отчёт
- **Фоновые задачи** (`app/workers/`): `setup_scanner` (30 мин, H1/H4), `position_monitor`
  (5 мин), `daily_jobs` (15 мин). Дедуп сигналов по слоту, TTL 4 часа
- **Графики**: `charting.py`, mplfinance через `asyncio.to_thread`, `threading.Lock`
  вокруг рендера. Уровни и EMA обрезаются окном видимых свечей ± 1 ATR
- **Экран «🔎 Анализ рынка»**: график всегда, вердикт H1/H4 теми же детекторами, что
  у сканера (`app/analysis/classify.py`). **Кнопки «Открыть сделку» нет и не будет**
- **Геометрия стопа** (`d9a37a2`): `BreakoutRetest` — `stop = min(candle.low, level) − 0.2·ATR`,
  `EMAPullback` — относительно `ema50`; инвариант `validate_geometry` в `signals.py`
- **Обработка ошибок**: `ErrorMiddleware` пишет трейс на ERROR, пользователю —
  обобщённое сообщение. Широкие `except Exception` в разделе «Биржа» сужены до `ExchangeError`
- `app/bot/messaging.py`: `edit_or_replace` — под фото `edit_text` падает
- **`build_fingerprint` не трогать** — разошлёт повторные уведомления

## Этап 15 — исполнение сделок по подтверждению

Решения: вход маркетом, SL и TP на биржу сразу, объём авто из риск-профиля с округлением
вниз, только из карточки READY, автоматического входа без человека нет, идемпотентность
через детерминированный `client_order_id` + запись `PENDING` до HTTP.

### Реализовано

- `app/execution/`: `models.py`, `sizing.py`, `guards.py`, `service.py`, `leverage.py`
- `app/services/permissions.py`, `app/services/position_mode.py`
- `app/bot/handlers/execution.py`: карточка, TTL 60 с, пересчёт при дрейфе
- `app/core/locks.py`: `RedisLock` с Lua compare-and-delete. **TTL лока 173 с**
  (`Settings.confirm_lock_ttl_seconds`) по формуле
  `ceil(http_timeout × (confirm_path_http_calls + 1)) + margin + read-back`: 15 запросов
  худшего пути «Да» с 15.5.4а. Число обновляется в том шаге, где добавляется запрос
- `app/workers/execution_digest.py`: сводка, правила аномалий, пороги выборки
- Статусы `execution_orders`: `DRY_RUN`, `REFUSED`, `DECLINED`, `EXPIRED`, `ERROR` +
  исходные. `status` — `VARCHAR(16)`, не enum
- `exchange_credentials.mode` (`LIVE`/`DEMO`), `user_settings.active_exchange_mode`.
  Куда уходят ордера — решает `BINGX_TRADING_MODE`

### Гейты (15.5.1)

- `TRADING_EXECUTION_ENABLED` — главный выключатель
- `EXEC_DRY_RUN` — дефолт `true`. **`false` роняет запуск валидатором `Settings`** до шага
  15.5.2 (проверено живым запуском)
- `EXEC_ALLOW_LIVE_MODE_ORDERS` — дефолт `false`. При `BINGX_TRADING_MODE=live` и
  выключенном флаге гвард `LIVE_ORDERS_NOT_ALLOWED` отказывает до сборки ордера

### Порядок проверок в `evaluate()`

`EXECUTION_DISABLED` → `LIVE_ORDERS_NOT_ALLOWED` → `PERMISSIONS_UNKNOWN` →
`POSITION_MODE_UNKNOWN` → `NO_TRADING_KEY` → `MODE_NOT_ALLOWED` → остальные гварды
раздела 7, включая `SIGNAL_STALE`, `SYMBOL_DATA_UNAVAILABLE`, `SYMBOL_NOT_ALLOWED`.

`POSITION_EXISTS` сравнивает **только символ**, сторона намеренно не участвует: в one-way
встречный ордер нетто-закрывает позицию, в хедже встречный вход — хеджирование вне ТЗ.
Закреплено тестом-замком.

### Шаги

15.1 ✅ → 15.2 ✅ → 15.3 ✅ → 15.4а–в ✅ → проверка прав ✅ → пакеты A/B/C ✅ →
15.5.1 ✅ → 15.5.1а ✅ → 15.5.2 отправка ✅ → 15.5.3 read-back и спасение стопа ✅ →
15.5.4 журнал из факта ✅ → 15.5.4а гвард живой позиции ✅ → 15.5.5 первый демо-ордер ✅ →
**15.6 reconciler ✅** (прод 28.09, `41a720f`, миграция `df411b3ca043`; отчёт по
демо-периоду — не сделан) → 15.7 боевой счёт.

### Reconciler (15.6)

Решения — `app/execution/reconciler.py` без I/O, запросы/журнал/события —
`app/workers/reconciler.py`, задача раз в 60 с. Журнал — история, биржа — истина по
текущему состоянию. Выход пишется только фактом биржи (закрывающие исполненные ордера
из `allOrders`, объём ровно равен уменьшению позиции; стоп/тейк — по `triggerOrderId`
дочернего ордера), иначе — событие `reconciliation_events` и одно уведомление без правки
журнала. UNKNOWN/PENDING старше 10 минут — поиск по `client_order_id`, `NOT_PLACED` при
«order not exist» без позиции. Ордеров не отправляет никогда; пропускает цикл при живом
`exec:lock:*`. Подробно — раздел 10 ТЗ.

**15.7**: обычный риск-процент (решение владельца, результат сделок — его риск), но
`EXEC_MAX_OPEN_POSITIONS=1` и один символ; расширение после 5 чистых исполнений подряд.
Детали — в handoff.

## Символы

`DEFAULT_ALLOWED_SYMBOLS` в `app/database/models/trading_plan.py`: BTC, ETH, SOL, BNB, XRP,
DOGE, ADA, AVAX, LINK, GRAMTON. Дефолт `TradingPlan.allowed_symbols` — per-user, не hard-cap.
Потребители: сканер и ручной выбор в интерфейсе. `exec_symbol_whitelist` — отдельный
список, только в `guards.py`.

## Вторая биржа — что готово, что нет

**Готово.** `ExchangeClient` — абстрактный контракт, ответы нормализованы в датаклассы
(`Ticker`, `Kline`, `Balance`, `Position`, `SymbolInfo`, `OrderResult`, `ApiRestrictions`,
`OpenOrder`, `AttachedTpSl`, `LeverageInfo`, режим позиций). Свойства биржи — на клиенте.

**Доделать.** Реестр клиентов в `ExchangeFactory`; блок настроек на биржу; канонический
формат символа (`BTC-USDT` против `BTCUSDT`); правила `client_order_id` по бирже.
Разумный момент — после 15.7.

## Ключевые уроки (не повторять ошибки)

- **Мок должен повторять реальный ответ биржи, а не документацию**
- **Форму ответа проверять для каждой ручки отдельно.** `apiRestrictions` без `code`/`msg`,
  режим позиций под `data`, плечо — v2, режим — v1
- **Фикстуру снимать с живого ответа, а не собирать руками**
- **Не печатать `repr()` ответа внешнего API вслепую.** Скан логов на секреты — только счётчики
- **Молчаливый фолбэк — это баг.** `data[0] if data else {}`, `default=True`,
  `item.get("maxLongLeverage", 20)`, выброшенный `extra` в логах
- **Подпись может врать при сходящейся арифметике**
- **Гвард, сравнивающий переменную с ней самой, холостой**
- **Замысел гварда важнее буквального текста**
- **Окно отчёта и момент отправки должны быть согласованы**
- **Правило про долю обязано иметь минимум выборки**
- **Спеку править вместе с кодом.** Разошедшаяся спека хуже отсутствующей
- **Документ о состоянии прода — не источник истины.** Перед деплоем md5 на сервере
- **Разведка ничего не отправляет на биржу**
- **«Снять ордер» — двусмысленно.** Говорить «прочитать» или «отменить»
- **Состояние, живущее в теряющемся объекте, — отсутствующее состояние**
- **Приватный кэш аккаунта не смешивать с публичным market-data кэшем**
- **Фильтр секретов — на хендлере, не на логгере**
- **Константа «число запросов в пути» — фактический путь, не запас**
- **Тестовые данные не должны утаскивать код в сеть**
- **Округление — задача слоя вывода.** Режим округления задавать явно
- **Тест, зависящий от переменной окружения, врёт.** Настройки передавать конструктором
- **Прогон без нуля skipped ничего не доказывает**
- **Измерять, а не прикидывать**
- **Одна метрика — одна команда** (периметр ruff дал расхождение на два порядка)
- **Тест-замок называть замком**, а не выдавать за упавший
- **Перед прод-миграцией показывать SQL офлайн-режимом alembic**
- **Скрипты, пишущие в БД коммитами, обязаны убирать за собой**; гвард по имени БД
- **Фейки харнесса дрейфуют вместе с сигнатурами** и маскируют друг друга каскадом
- **`core.autocrlf=true` ломает `git archive`** — лить с `-c core.autocrlf=false`
- **Шаг, зависящий от предыдущего, — через `&&`, не `;`**
- Тикеры, адреса и ручки проверять живым запросом. Toncoin — `GRAMTON-USDT`
- Прод-миграцию — только после явного «да» и свежего дампа; `downgrade` работает всегда
- Новые настройки в `Settings` обязаны иметь дефолты
- `grep -q` с `pipefail` даёт ложный сбой — `grep -c`
- `194.87.133.54` — чужой сервер, протухший `known_hosts`. Не подключаться

## Рабочий процесс

- Архитектура и решения — чат с Opus. Файлы и деплой — Claude Code:
  `cd C:\Users\vladz\OneDrive\Desktop\trading` → `claude`
- Промпты для Claude Code — одним блоком, копируется одной кнопкой
- `/clear` между шагами. Разведку и реализацию не смешивать
- **План по файлам до кода** на всём, что трогает больше двух модулей
- Прод-деплой блокируется гвардом среды и требует явного подтверждения
- Сеть рвётся (VPN). При обрыве посреди правки — `git status` и `git diff --stat`

### Отложено осознанно

- Тихие часы — схема готова, реализации нет
- Третья стратегия — сначала статистика по двум
- M15 — отклонён, шум
- Закрытие позиции кнопкой, трейлинг, безубыток — после этапа 15
- Вход не по сигналу — этап 16, через `execution/service.py`
- Telegram Mini App для журнала — после 15.7

### Этапы 13–14

- Healthcheck контейнеров, **ротация логов**, автоперезапуск
- Аудит безопасности: порты, SSH, права на `.env`, обновления системы

### Монетизация (после всего)

Пользовательские AI-ключи, тарифы, платежи. Не раньше, чем исполнение отработает на
собственных деньгах.

### Мелкие хвосты

- `environment=dev` на проде — выяснить, что читает
- Хардкод «USDT» в текстах, `UserSettings.quote_currency` никто не читает
- Мёртвый код: `AnnotateTradeStates`, `if …: pass` в `setups.py:339-345`
- Разнобой в `insights_menu()` по конвенции навигации
- Мусорные папки `%SystemDrive%` и `Python` в корне проекта (untracked)
- Кириллический шум `RUF001-003`; `RUF100`=4, `I001`=10 по `ruff check .`
