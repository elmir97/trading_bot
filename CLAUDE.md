# Trading Journal Bot — рабочие заметки

Telegram-бот журнала сделок и поиска сетапов на фьючерсах BingX.
Один пользователь. Архитектура и решения — `docs/architecture.md`, текущее
состояние, очередь, хвосты и история — `docs/handoff.md`, ТЗ этапа 15 —
`docs/execution-stage-15.md`. Папка `docs/` в образ не попадает (`.dockerignore`).

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

Ориентир на 01.10.2026 (--fault data-change репетиции миграции): 1506 passed, 0 skipped, 0 failed.

Число тестов в этом файле — ориентир на момент записи, а не факт. Перед
тем как называть его в плане или отчёте, прогонять пакет и брать свежую
цифру.

**Перед каждым push — ПОЛНЫЙ pytest (0 failed, 0 skipped), не только тесты,
затронутые правкой.** Урок 28.09, дважды за день: `376d8ad` запушен с двумя
падениями в `test_scanner_commit.py` (фейк контекста — `object()`, сканер начал
читать `context.atr`; гонялись только тесты сканера), утром `a5c1ad4` — со
сломанным smoke (см. «Скрипты»). Падения в «чужих» файлах ловит только полный
пакет.

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
перестают быть сравнимы (см. `docs/handoff.md`, архив 22.09: старые «`RUF100`,
`I001` — 0, ~12 `E501`» снимались неизвестным другим периметром).

## Скрипты

`scripts/smoke_check.py` — 67 проверок, гоняет диспетчер. Единственный
харнесс для хендлеров `settings.py`.

- отказывается работать против любой БД кроме `trading_bot_test`
- отказывается работать без `TRADING_EXECUTION_ENABLED`
- пишет настоящими коммитами и убирает за собой в `finally`
- сеть запрещена целиком (httpx sync/async, aiohttp): любой запрос —
  строка `✗ СЕТЬ: <метод> <url>` и exit 1, даже если проверки прошли
- `telegram_id=424242` зарезервирован под него, не использовать ни подо
  что ещё
- проверка исправности — два прогона подряд без ручной уборки
- **коммит, который добавляет запрос к бирже на пути карточки/«Да», прогоняет
  smoke в том же коммите** и добавляет заглушку метода; pytest этого не ловит
  (28.09: `a5c1ad4` запушен с падающим smoke — нет заглушки `get_margin_type`)

`scripts/signal_outcomes.py` — отчёт исходов READY-сигналов, запускать на проде
**только с разрешения владельца**:

```
docker compose exec -T bot python -m scripts.signal_outcomes [--horizon 50] [--json out.json]
```

- только чтение: SELECT в транзакции `READ ONLY` + публичные свечи BingX (GET klines),
  ключи не нужны; остаток лимита klines ≤ 2 — стоп без отчёта (exit 2)
- исход стоп/тейк/открыт по закрытым свечам после `notified_at`, горизонт 50; оба уровня
  в одной свече — стоп; R от RR снимка, нетто — минус taker-комиссия в R
  (`exec_taker_fee_rate`); открытые вне среднего R
- признаки — из снимка (миграция `ea93de72860d`), до неё — прогоном детектора
  (`replay` совпал со снимком / `replay≠`); срезы по ТФ, сетапу, направлению,
  порогам признаков и «один сигнал на пробой» (`breakout_at`)
- `--json` пишет файл внутри контейнера — забрать и удалить

`scripts/rehearse_migration.sh` — репетиция миграции на копии прода, запуск и отчёт —
«Деплой» → «Репетиция миграции».

- на проде только читает: `pg_dump` и SELECT (`count`, `pg_database_size`) в контейнере
  `trading_bot_db` с `default_transaction_read_only=on`
- запускать **только из HEAD через stdin** (`git show HEAD:…`), не из
  `/opt/trading_bot/scripts`: там версия прошлого деплоя. В образ `.sh` не попадают
  (`.dockerignore`), CRLF им запрещён (`.gitattributes`)
- docker — только через `dk()` (`</dev/null` внутри), alembic — только через `al()`
  (проверка адреса в том же контейнере), последняя строка `main "$@"; exit $?` —
  `tests/test_rehearse_migration_script.py` это проверяет. Правка скрипта —
  `shellcheck scripts/rehearse_migration.sh` (из `shellcheck-py`, на этой машине
  `python -m pip install -r requirements-dev.txt`) и этот тест
- **весь SQL скрипта** (всё, что уходит в `src_sql`/`copy_sql`) тест прогоняет тем же
  `psql` с теми же флагами, в READ ONLY, на `trading_bot_test`; новый запрос обязан
  попасть в его список. Прогон с поддельным `docker` SQL не выполняет — 01.10 так
  до сервера дошли `ORDER BY 1 COLLATE "C"` и `text || "char"`
- коды выхода — одно правило: **2 — отказ по проверке входа** (аргументы, предусловия:
  команды, docker, остатки, источник, RAM, диск, образ, ревизии в образе; ревизия
  копии ≠ `--from`); **1 — любой сбой выполнения** (упала команда: docker, `pg_dump`,
  restore, psql, alembic) **и провал проверок репетиции** (защита адреса, ревизия,
  строки, схема после шага); 0 OK; 3 — уборка неполная (перекрывает 0/1/2). Лог
  `/opt/backups/rehearsal_<ts>.log` (600) уборка не удаляет — забрать и удалить
- флаги проверки самого скрипта: `--source-container <не прод>`; `--fault wrong-db`
  (защита адреса, ждать exit 1) и `--fault data-change` (после первого upgrade в копии
  меняется одна строка text-колонки, только с `--checksum`; ждать exit 1 и ❌ в таблице
  md5) — оба только с `--source-container` не прода и без `--rewind-to`; `--rewind-to
  <rev>` (репетиция уже стоящей миграции)
- текст SQL в скрипте — только ASCII (тест): psql на Windows получает argv в cp1251

`scripts/check_redis.py` — PING и цикл проверок лока, запускать на проде:

```
docker compose exec bot python -m scripts.check_redis
```

## Деплой

Сервер `root@147.45.111.10`, проект в `/opt/trading_bot`. SSH-ключ на
машине настроен.

Бот — `@postfactumTrade_bot`, id 8867032918 (с 28.09; прежний
`@elmir_trading_journal_bot`, id 8690367328 — бот удалён 28.09, токен недействителен). Токен и прочие секреты
приходят в контейнер только через `env_file: .env`, в образе их нет (`.dockerignore`).
Смена токена — правка `/opt/trading_bot/.env` + `docker compose up -d --no-deps
--force-recreate bot`, без пересборки. Токен не печатать: только префикс до `:` (id
бота), длина, sha256[:8].

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
   а md5 не сходятся с коммитом.
   `tar` под Windows (Git Bash) принимает `C:` в пути за удалённый хост
   («Cannot connect to C: resolve failed») — `tar --force-local` или
   относительный путь.
   GNU tar: `--exclude` — **до** пути (`tar czf X --exclude=trading_bot/logs -C /opt
   trading_bot`); после пути он не действует, и tar выходит с ошибкой (28.09,
   снапшот пересоздавали)
3. Сверить md5 изменённых файлов с `git show HEAD:<path>`
4. **Точка отката — ДО build.** Проверка, а не вера: id обязан
   напечататься, иначе стоп, build не запускать
   ```
   TS=$(date -u +%Y%m%d_%H%M%S)
   docker tag trading_bot-bot:latest trading_bot-bot:rollback_$TS
   docker image inspect trading_bot-bot:rollback_$TS --format '{{.Id}}'
   ```
   Без тега старый образ после сборки становится безымянным и может
   исчезнуть (27.09: `b11e08849a42` пропал между build и stop — отката
   без пересборки не осталось)
5. `docker compose up -d --build`
6. `alembic upgrade head`
7. Показать `docker compose ps` и последние 50 строк логов

Откат кода: `docker tag trading_bot-bot:rollback_<ts> trading_bot-bot:latest
&& docker compose up -d --no-deps bot`. Удалять rollback-тег
(`docker rmi trading_bot-bot:rollback_<ts>`) — только после первого
чистого «Цикл сканера завершён» на новом образе и с «да» владельца.

**Миграции на проде — только после явного «да» владельца.** Порядок:
свежий дамп → показать SQL офлайн-режимом alembic (без подключения
к базе) → дождаться «да» → применять. Если alembic применяет миграцию,
которой не ждали — остановиться и сказать.

### Деплой с миграцией

Upgrade идёт одной транзакцией: `alembic/env.py` оборачивает все миграции
одним `context.begin_transaction()`, `transaction_per_migration` не задан.
Падение посреди бэкфилла откатывает всё целиком.

0. Репетиция на копии прода `scripts/rehearse_migration.sh` (ниже) — `ИТОГ: OK`,
   отчёт принят владельцем
1. Чек-ап прода, шаги 1-4 обычного деплоя (umask, дамп, снапшот, код, md5,
   **точка отката `rollback_<ts>` с проверкой `docker image inspect`**)
2. `docker compose build bot` — пока старый бот работает
3. `docker compose run --rm -T --no-deps bot alembic upgrade <current>:<new> --sql`
   — показать SQL, ждать «да». Из НОВОГО образа: в работающем контейнере
   старый код, файла миграции там нет
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
docker compose run --rm --no-deps bot alembic downgrade <rev>  # <rev> — ревизия до деплоя; НОВЫЙ образ — в нём файл миграции
docker compose run --rm --no-deps bot alembic current          # == <rev>
docker tag trading_bot-bot:rollback_<ts> trading_bot-bot:latest   # образ шага 1
docker compose up -d --no-deps bot
docker compose logs --tail 50 bot
```

Код на диске откатить из снапшота шага 1 (распаковать поверх
/opt/trading_bot) — иначе следующая сборка снова соберёт новый код. Если
rollback-тега нет — только так: снапшот и `docker compose build bot`.
Nullable-колонка без бэкфилла downgrade не требует: старый код с ней
работает, откатывается только образ.

Порядок важен: сначала downgrade новым образом (старый образ не знает
файла миграции), и только потом старый код. Downgrade — к явной ревизии,
не `-1`: при двух миграциях за деплой `-1` откатит одну, а репетиция
прогоняла откат именно к `<rev>`. Если шаг 6 упал целиком,
транзакция уже откатилась — `alembic current` покажет старую ревизию,
downgrade не нужен.

### Репетиция миграции

До деплоя, на сервере, данные не покидают его. Две команды из Git Bash, из
корня `trading_bot`, на закоммиченном HEAD. Перед ними — показать владельцу
и получить «да».

1. Образ нового кода — tar `git archive` прямо в `docker build` (контекст из
   stdin, на диск сервера ничего, `/opt/trading_bot` и боевой образ не
   тронуты). Сначала свободная RAM: VPS 1.9 ГБ без swap, рядом работает бот —
   меньше 700 МБ available, и сборка не запускается
   ```
   git -c core.autocrlf=false archive HEAD | ssh root@147.45.111.10 'm=$(free -m | grep "^Mem:" | tr -s " " | cut -d" " -f7); echo "RAM available: $m MB"; [ "$m" -ge 700 ] && exec docker build -t trading_bot:rehearsal -'
   ```
2. Репетиция. `--from` — ревизия прода (`alembic current`, чек-ап пункт 7)
   ```
   git -c core.autocrlf=false show HEAD:scripts/rehearse_migration.sh | ssh root@147.45.111.10 bash -s -- --from <rev> --image trading_bot:rehearsal --checksum [--expect-columns t.c,…] [--expect-null t.c,…] [--allow-data-change t.c,…]
   ```

Что делает скрипт: дамп прода `--no-owner --no-privileges` →
`/opt/backups/rehearsal_<ts>.sql.gz` (600, `gzip -t`); копия — `postgres:16-alpine`
(образ сверяется с контейнером прода) в tmpfs = max(256 МБ, 3 × размер базы),
`--memory 256m`, `fsync=off`, своя `--internal`-сеть, без `-p`; restore с
`ON_ERROR_STOP=1`; ревизия копии == `--from`, иначе отказ; upgrade → downgrade →
upgrade → downgrade к явной ревизии `--from` (второй downgrade — прогон отката,
`--no-final-downgrade` убирает). Alembic — `docker run` образа репетиции в сети
копии, без `.env` и compose; перед каждым шагом тот же контейнер сверяет
`current_database()` = `trading_bot_rehearsal` и `inet_server_addr()` = адрес копии,
иначе alembic не запускается. После каждого шага: строк `Running upgrade/downgrade`
ровно по длине цепочки, `alembic_version`, `count(*)` всех таблиц = baseline
(`--allow-count-change t`), нормализованный снимок схемы (колонки по имени,
индексы, ограничения) после downgrade = baseline, после второго upgrade = первому
(`--allow-schema-diff таблица[.имя]`). С `--checksum` — md5 строк каждой таблицы
по колонкам baseline (`ROW(…)::text`, строки по тексту, collation "C") после
каждого downgrade = baseline; колонки, которые downgrade законно меняет
(например вид события → `AMBIGUOUS`), — `--allow-data-change таблица.колонка`.
`--expect-null таблица.колонка` — после каждого upgrade колонка целиком NULL
(новая, без бэкфилла). Уборка в `trap` при любом выходе:
контейнер, сеть, тег `trading_bot:rehearsal` (`rmi` без `-f`), дамп — и
проверка, что их нет. Обрыв ssh уборку не прерывает, вывод дублируется в
`/opt/backups/rehearsal_<ts>.log`.

Отчёт принят, если: `ИТОГ: OK` и exit 0; во всех столбцах таблицы строк числа
равны baseline (столбец прода — для информации, бот пишет во время дампа); в
таблице md5 после downgrade нет ❌; «Схема baseline → upgrade 1» совпадает с
офлайн-SQL миграции. Exit 2 — условия
не позволили начать (причина в ИТОГ), exit 1 — сбой или провал репетиции: стоп,
вывод владельцу, не чинить на ходу. Exit 3 — показать остатки владельцу, руками
не чистить без «да». Лог забрать в отчёт и удалить.

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

**Замер лимитов — тоже нагрузка.** Один запрос на ручку, `X-RateLimit-Requests-Remain`/
`-Expire` брать из его ответа; серии подряд не делать. 28.09 три GET `trade/marginType`
за секунду (лимит 2/1 с) → код 100410, ручка заблокирована для ключа ~5 мин — карточка
в это окно отказала бы `MARGIN_MODE_UNKNOWN`. Окна (демо, 28.09): приватные — 1 с
(openOrders 5, positions 10, marginType 2, leverage 5, order GET 30 / POST ~10,
balance 40), ticker — 10 с (500).

**Скрипты на сервер — только через stdin** (`ssh … python - < file` или heredoc), не
строкой с экранированием внутри `ssh '…'`: 28.09 экранирование дважды дало ложные
числа.

**Bash-скрипт через `ssh … 'bash -s' < file`: каждой docker-команде внутри — `</dev/null`**
(`docker compose exec -T`, `docker compose run -T`, `docker run`, `docker exec` без
`-i`). Иначе она читает stdin и съедает остаток скрипта: 28.09 репетиция миграции
вышла молча сразу после `docker compose exec -T postgres pg_dump`, без ошибки. Команде,
которой stdin нужен (`zcat dump | docker exec -i … psql`), — пайп, как обычно.

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
   `exec_margin_type_ttl_seconds` (с блоков 28.09),
   `exec_daily_digest_hour`, `log_json`, `environment`,
   `reconciler_notify_max_age_hours`, `reconciler_pulse_every` (с `c8306f9`/`90fae6e`).
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
    длительность). Строки «Скан рынка» в логах нет. С `27ac9db` рассылки
    daily_jobs пишут INFO `Рассылка отправлена: daily_report|daily_limit_reached|
    execution_digest`, сбой — WARNING `Рассылка не доставлена…` / `…выброшена`;
    на коде до него — только `user_settings.execution_digest_last_sent_date`
    (пункт 14). С `90fae6e` reconciler пишет `Пульс reconciler: …` раз в
    `reconciler_pulse_every` запусков (60 — раз в час): последний не старше часа,
    `ошибок` и `пропущено по локу` — флаг, если не 0. Плюс `Лимит BingX после POST`
    (остаток и окно лимитов пути входа, с `e194bb7`) — копить в handoff

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
    дата, если час `EXEC_DAILY_DIGEST_HOUR` по локальному времени прошёл.
    С миграции `7b4e2c9a1f35`: `reconciliation_events` с `notified_at IS NULL`
    — недоставленные уведомления (`gave_up_at` NULL — ещё в переотправке, не
    NULL — отказ); по `kind`, с `attempts` и возрастом
15. `signal_notifications` с `level='READY'` по `notified_at` за 24 ч и с
    окна деплоя (события, не слоты); сколько из них дошло до карточки —
    distinct `notification_id` в `execution_orders`, кроме `REFUSED` на
    `card`. Плюс уведомления с `trade_opened_at IS NOT NULL`.
    `signals.trade_opened_at` с 15.5.2а не используется
16. `trades` со `status='OPEN'` по `source`; всего
    `source='SIGNAL_EXECUTION'`; из них `fill_confirmed = false`
    (предварительные, 15.5.4) — при сухом прогоне их быть не должно
16а. С миграции `0bf103d4a84d`: `execution_callbacks` с окна деплоя по
    `action`; `notification_id IS NULL` (битые кнопки) — флаг. Каждому
    `yes` — строка исхода в `execution_orders` по тому же
    `notification_id`; `yes` без исхода — только двойной тап под локом
    (пара `yes` по одному уведомлению за секунды). В логе — WARNING/ERROR
    «Нажатие кнопки исполнения не записано» — флаг: «Да» по нему отказало.
    `telegram_id` в логе — только маской `mask_telegram_id`

### F. Redis

`scripts/check_redis` **пишет** (цикл захвата/снятия лока на своём
ключе) — в чек-апе его не запускать. Вместо него инлайн через
`redis.asyncio.Redis.from_url(get_settings().redis_url)`:

17. `PING`; `INFO memory` — `used_memory_human`, `maxmemory_human`
    (256M), `maxmemory_policy` (`noeviction`)
18. `SCAN exec:lock:*` — счётчик и `TTL` каждого. `TTL` -1 или больше
    `confirm_lock_ttl_seconds` (187 — прод с `eea28cc`, 28.09: сон троттлера в формуле;
    183 — блоки 28.09; 173 — 15.5.4а; 163 — 15.5.3) — залипший лок

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
