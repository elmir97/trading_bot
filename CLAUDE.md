# Trading Journal Bot — рабочие заметки

Telegram-бот журнала сделок и поиска сетапов на фьючерсах BingX.
Один пользователь. Архитектура и история решений — вне репозитория.

## Локальный прогон

Docker на этой машине **не работает** (виртуализация выключена в BIOS).
Docker Desktop не запускать, профиль `test` из compose не поднимать.

Тестовая БД — нативный PostgreSQL 18, служба `postgresql-x64-18`:

```
DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/trading_bot_test
```

База `trading_bot_test` и роль `test`/`test` созданы, миграции применены.
`.env` локально НЕ создавать — всё переменными окружения запуска.

Нужны: `DATABASE_URL`, `BOT_TOKEN`, `ENCRYPTION_KEY`,
`TRADING_EXECUTION_ENABLED=true`. Для `smoke_check.py` на Windows-консоли
ещё `PYTHONUTF8=1`.

**Прогон зелёный только при нуле skipped.** Без `DATABASE_URL` молча
пропускается ~115 интеграционных тестов, и счёт врёт.

Ориентир: 625 passed, 0 skipped, 0 failed.

Число тестов в этом файле — ориентир на момент записи, а не факт. Перед
тем как называть его в плане или отчёте, прогонять пакет и брать свежую
цифру.

## Линт

Эталонный периметр и команда для цифр ruff — весь репозиторий, из корня
`trading_bot`:

```
ruff check .
```

Разбивка по кодам — `ruff check . --statistics`. Конфиг в `pyproject.toml`
(`[tool.ruff]`/`[tool.ruff.lint]`), per-file/per-directory исключений нет.

Ориентир на 22.09.2026 (`1703d71`): 2408 ошибок, почти всё `RUF001`-`RUF003`
(кириллица в докстрингах и комментариях); `F821`=0, `RUF100`=4, `I001`=10,
`E501`=55.

Число — ориентир на момент записи, а не факт. Перед тем как называть его
в плане или отчёте, прогонять `ruff check .` той же командой и брать
свежую цифру — иначе периметр молча съезжает и цифры между прогонами
перестают быть сравнимы (см. `handoff-2026-09-22.md`: старые «`RUF100`,
`I001` — 0, ~12 `E501`» снимались неизвестным другим периметром).

## Скрипты

`scripts/smoke_check.py` — 66 проверок, гоняет диспетчер. Единственный
харнесс для хендлеров `settings.py`.

- отказывается работать против любой БД кроме `trading_bot_test`
- отказывается работать без `TRADING_EXECUTION_ENABLED`
- пишет настоящими коммитами и убирает за собой в `finally`
- `telegram_id=424242` зарезервирован под него, не использовать ни подо
  что ещё
- проверка исправности — два прогона подряд без ручной уборки

`scripts/check_redis.py` — PING и цикл проверок лока, запускать на проде:

```
docker compose exec bot python -m scripts.check_redis
```

## Деплой

Сервер `root@147.45.111.10`, проект в `/opt/trading_bot`. SSH-ключ на
машине настроен.

Remote `origin` — приватный резервный репозиторий на GitHub
(`elmir97/trading_bot`), не деплойный канал. После каждого коммита —
`git push`. Деплой на сервер по-прежнему идёт через `git archive`
(шаг 2 ниже), push его не заменяет и не запускает.

1. `umask 077` первой командой сессии — до создания дампа и снапшота.
   Внутри снапшота лежит `.env`: файлы обязаны получиться 600 сами,
   без `chmod` задним числом. Затем дамп БД и снапшот кода:
   `/opt/backups/trading_bot_pre_deploy_<timestamp>.{sql.gz,tar.gz}`,
   `gzip -t` обоим, `stat -c '%a'` — 600.
   Права на проде: `.env` и `docker-compose.override.yml` — 600,
   `/opt/backups` и подкаталоги — 700, файлы внутри — 600 (кроме
   `scripts/backup_db.sh` — 700, его запускает cron root-а; в нём
   `umask 077` второй строкой)
2. Код лить через `git archive HEAD`, **исключая** `.env` и
   `docker-compose.override.yml` — они живут только на сервере.
   Обязательно `git -c core.autocrlf=false archive HEAD`: на этой
   машине `core.autocrlf=true`, и без флага файлы уезжают с CRLF,
   а md5 не сходятся с коммитом
3. Сверить md5 изменённых файлов с `git show HEAD:<path>`
4. `docker compose up -d --build`
5. `alembic upgrade head`
6. Показать `docker compose ps` и последние 50 строк логов

**Миграции на проде — только после явного «да» владельца.** Порядок:
свежий дамп → показать SQL офлайн-режимом alembic (без подключения
к базе) → дождаться «да» → применять. Если alembic применяет миграцию,
которой не ждали — остановиться и сказать.

### Деплой с миграцией

Upgrade идёт одной транзакцией: `alembic/env.py` оборачивает все миграции
одним `context.begin_transaction()`, `transaction_per_migration` не задан.
Падение посреди бэкфилла откатывает всё целиком.

0. Репетиция на копии прода (ниже) — пройдена, отчёт принят
1. Чек-ап прода, шаги 1-4 обычного деплоя (umask, дамп, снапшот, код, md5)
2. `docker compose exec -T bot alembic upgrade <current>:<new> --sql` —
   показать SQL, ждать «да»
3. `docker compose build bot` — пока старый бот работает
4. `docker compose stop bot` — старый код не должен ни рассылать, ни
   обрабатывать «Да» во время миграции
5. Проверки данных, от которых зависит миграция (SELECT)
6. `docker compose run --rm --no-deps bot alembic upgrade head` — ровно
   одна строка `Running upgrade`
7. Проверка схемы и данных (SELECT, по списку миграции)
8. `docker compose up -d --no-deps bot`, логи первой минуты, `grep -ci error`
9. Проверки после старта (конфиг, execution_orders без PENDING/SUBMITTED/
   UNKNOWN при сухом прогоне)
10. Ручная проверка владельцем — для 15.5.2а: старая кнопка отвечает
    «Уведомление устарело», «⚡ Открыть сделку» в новом уведомлении
    открывает карточку с теми же уровнями, что в тексте уведомления

**Откат**, если провалился шаг 6 или 7:

```
docker compose run --rm --no-deps bot alembic downgrade -1   # НОВЫЙ образ — в нём файл миграции
docker compose run --rm --no-deps bot alembic current        # == предыдущая ревизия
# код из снапшота шага 1, распаковать поверх /opt/trading_bot
docker compose build bot && docker compose up -d --no-deps bot
docker compose logs --tail 50 bot
```

Порядок важен: сначала downgrade новым образом (старый образ не знает
файла миграции), и только потом старый код. Если шаг 6 упал целиком,
транзакция уже откатилась — `alembic current` покажет старую ревизию,
downgrade не нужен.

### Репетиция миграции

До деплоя, на сервере, данные не покидают его. `umask 077`.

1. Свежий дамп прода `pg_dump --no-owner --no-privileges` →
   `/opt/backups/rehearsal_<ts>.sql.gz`, `gzip -t`, режим 600
2. Временная сеть `docker network create tb_rehearsal_net` и контейнер
   Postgres **того же образа, что в `docker-compose.yml` прода** (сейчас
   `postgres:16-alpine`), `--tmpfs` под данные, без `-p`. Восстановить дамп
   через `psql -v ON_ERROR_STOP=1`
3. Образ нового кода под отдельным тегом (`trading_bot:rehearsal`) из
   `git archive` во временном каталоге — боевой образ и `/opt/trading_bot`
   не трогать. Alembic запускать с `-e DATABASE_URL=…@<контейнер репетиции>`;
   перед каждым шагом `SELECT current_database(), inet_server_addr()` —
   адрес контейнера репетиции, иначе стоп
4. До миграции: данные, от которых она зависит; снимок значимых колонок
5. `alembic upgrade head` → проверки схемы и данных
6. `downgrade -1` → схема вернулась, данные не потеряны → снова `upgrade head`
7. Ещё раз `downgrade -1` с проверкой — это прогон процедуры отката
8. Уборка: контейнер, сеть, образ, временный каталог, дамп репетиции.
   Проверить, что ничего не осталось

Переменные окружения на проде добавлять через
`docker-compose.override.yml`, не правкой `.env`. Исключение —
инфраструктурные адреса вроде `REDIS_URL`: им место в `environment`
сервиса в `docker-compose.yml`.

## Разведскрипты к бирже

Одноразовый скрипт в контейнере бота, credentials брать через
`ExchangeFactory` — код сам расшифрует, ключ не покинет процесс.
`psql` на проде не нужен и блокируется.

**Ключи никогда не передавать в чат.** Ответ внешнего API печатать
полями по известному списку, не `repr()` целиком: одна ручка BingX
возвращает `apiKey` эхом.

Скрипт после прогона удалять из контейнера, с сервера и локально.

## Чек-ап прода перед деплоем

**Строго только чтение.** Без рестартов, без записи в БД и Redis, без
правок файлов на сервере, на биржу только GET. Ничего не чинить — только
отчёт. Разовые скрипты целиком инлайн, на диск сервера ничего:

```
ssh root@147.45.111.10 'cd /opt/trading_bot && docker compose exec -T bot python -' <<'PY'
...
PY
```

Секреты, ключи и `repr()` ответов не печатать — только поля по списку.
Зависимые шаги — через `&&` или отдельными вызовами, не через `;`.
«Окно деплоя» ниже — `State.StartedAt` контейнера `trading_bot` из
пункта 1, а не дата из памяти.

### A. Сервер

1. `docker compose ps`; для каждого контейнера `docker inspect --format
   '{{.Name}} {{.RestartCount}} {{.State.StartedAt}} {{.State.Health}}'`.
   `RestartCount` > 0 после деплоя — флаг. У `trading_bot` healthcheck
   нет (`health=none`) — это норма
2. `df -h / /opt`, `free -m`, `uptime`; `du -sh /opt/backups
   /opt/trading_bot/logs`; `docker system df`. Диск VPS — 28 ГБ
   (не 40), RAM ~1.9 ГБ, swap нет
3. `timedatectl` — `System clock synchronized: yes` (иначе подпись BingX
   начнёт отказывать)
4. `ss -tulpn` — 5432 и 6379 на хосте не слушают; `ufw status`
5. `stat -c '%a %n' .env docker-compose.override.yml`;
   `find /opt/backups -maxdepth 1 -type f ! -perm 600` — и для `.tar.gz`
   из списка `tar tzf | grep -E '(^|/)\.env$'` (есть ли внутри `.env`)

### B. Код на проде

6. Локально: для каждого файла `git ls-tree -r --name-only <commit> app`
   посчитать `git -c core.autocrlf=false show <commit>:<path> | md5sum`,
   список передать по stdin в `md5sum -c --quiet -` в `/opt/trading_bot`.
   Плюс сравнить списки файлов (лишние файлы на проде). `<commit>` —
   последний задеплоенный, его время коммита сверить со `StartedAt`
7. `docker compose exec -T bot alembic current` == `alembic heads`

### C. Конфиг

8. Из `get_settings()` печатать только: `trading_execution_enabled`,
   `exec_dry_run`, `exec_allow_live_mode_orders`, `bingx_trading_mode`,
   `bingx_base_url`, `bingx_demo_base_url`, `exec_symbol_whitelist`,
   `exec_max_open_positions`, `exec_max_total_risk_percent`,
   `confirm_lock_ttl_seconds`, `exec_position_mode_ttl_seconds`,
   `exec_daily_digest_hour`, `log_json`, `environment`.
   `bingx_base_url` — только публичный клиент; ключевой клиент в режиме
   demo ходит на `bingx_demo_base_url`

### D. Логи за 24 часа

`logs/bot.log` пишет **только время, без даты**, и ротируется. Даты
восстанавливать проходом с конца файла (конец = сегодня): переход
времени вверх больше чем на 12 часов — предыдущие сутки. Для текущего
контейнера сверять с `docker logs -t --since 24h trading_bot`. Формат
строки — `HH:MM:SS | LEVEL | logger | message | extra`, в awk
разделитель `-F' [|] '`.

9. Счётчики по уровням и `уровень + логгер` для WARNING/ERROR; для
   ERROR — до 10 уникальных `message` (без хвоста extra)
10. `grep -c Traceback`
11. `grep -ci -e Conflict -e 'terminated by other getUpdates'` — второй
    экземпляр бота
12. Последние `Фоновый цикл завершён: setup_scanner|position_monitor|
    daily_jobs` и `Цикл сканера завершён` (символы, запросы,
    длительность). Строки «Скан рынка» в логах нет. Отправка сводки
    исполнения в лог не пишется — смотреть
    `user_settings.execution_digest_last_sent_date` (пункт 14)

### E. База

Только агрегаты, через `Database(get_settings()).engine.connect()` и
первым запросом `SET TRANSACTION READ ONLY`, в конце `rollback()`.
`execution_orders.stage` хранится в нижнем регистре (`card`/`confirm`),
`status` — в верхнем.

13. `pg_size_pretty(pg_database_size(current_database()))`; соединения
    из `pg_stat_activity` по `state`
14. `execution_orders` с окна деплоя: `status × stage`. Любая строка
    `PENDING`/`SUBMITTED`/`UNKNOWN` при `exec_dry_run=true` — флаг.
    Там же `user_settings.execution_digest_last_sent_date` — вчерашняя
    дата, если час `EXEC_DAILY_DIGEST_HOUR` по локальному времени прошёл
15. `signal_notifications` с `level='READY'` по `notified_at` за 24 ч и с
    окна деплоя (события, не слоты); сколько из них дошло до карточки —
    distinct `notification_id` в `execution_orders`, кроме `REFUSED` на
    `card`. Плюс уведомления с `trade_opened_at IS NOT NULL`.
    `signals.trade_opened_at` с 15.5.2а не используется
16. `trades` со `status='OPEN'` по `source`; всего
    `source='SIGNAL_EXECUTION'`; из них `fill_confirmed = false`
    (предварительные, 15.5.4) — при сухом прогоне их быть не должно

### F. Redis

`scripts/check_redis` **пишет** (цикл захвата/снятия лока на своём
ключе) — в чек-апе его не запускать. Вместо него инлайн через
`redis.asyncio.Redis.from_url(get_settings().redis_url)`:

17. `PING`; `INFO memory` — `used_memory_human`, `maxmemory_human`
    (256M), `maxmemory_policy` (`noeviction`)
18. `SCAN exec:lock:*` — счётчик и `TTL` каждого. `TTL` -1 или больше
    `confirm_lock_ttl_seconds` (163 с 15.5.3: read-back под локом) —
    залипший лок

### G. BingX (demo, только GET)

`ExchangeFactory(settings, SecretCipher(...)).for_user(session, 1,
mode=ExchangeKeyMode.DEMO)`. Отдельной DEMO-строки в
`exchange_credentials` может не быть — ключи BingX общие для режимов,
фабрика берёт LIVE-строку. Проверить, что `client._base_url` — демо-хост.

19. `get_ticker("BTC-USDT")` — `last_price`, `timestamp`
20. `get_balance()` — `asset`, `equity`, `available`, `used_margin`
21. `get_api_restrictions()` — `enable_futures`,
    `permits_universal_transfer`, `ip_restrict`; и
    `exchange_credentials.permissions_checked_at`
22. `get_position_mode()` — `dual_side_position`
23. `get_leverage("BTC-USDT")` — long/short текущее и максимум

### H. Локально на HEAD

24. Полный `pytest` (passed/skipped/failed — skipped обязан быть 0),
    `smoke_check.py` дважды подряд с exit code, `python -m ruff check .
    --statistics` (`F821`, `RUF100`, `I001`, `E501`, итог),
    `python -m mypy app`. Всё — против последних известных чисел.
    `ruff`/`mypy` на этой машине только через `python -m`

### Отчёт

Одна таблица: пункт | результат | ✅ / ⚠️ / ❌. Под ней — только ⚠️ и ❌:
что увидел и чем это грозит ближайшему деплою. Предложения по починке —
отдельным списком. Ничего не исправлять в рамках чек-апа.

## Конвенции

- Настройки — через `get_settings()`. Модульного синглтона `settings` нет
- Форматирование чисел — `app/bot/formatting.py`. Внутри Decimal полной
  точности, округление только на выводе, `quantize()` всегда с явным
  `rounding=ROUND_HALF_UP`
- Настройки в тесты передавать явным конструктором `Settings(...)`,
  не полагаться на переменные окружения
- Форму ответа биржи проверять живым запросом, а не по документации.
  У разных ручек она разная: часть кладёт поля под `data`, часть
  на верхний уровень
- Молчаливый фолбэк вместо ошибки — баг. Если ожидаемого нет, падать
  внятно, а не подставлять первое попавшееся
- Новые настройки в `Settings` обязаны иметь дефолты
- `downgrade` в миграциях должен работать всегда
