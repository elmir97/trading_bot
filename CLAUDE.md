# Trading Journal Bot — рабочие заметки

Telegram-бот журнала сделок и поиска сетапов на фьючерсах BingX.
Один пользователь. Архитектура и решения — `docs/architecture.md`, текущее
состояние, очередь, хвосты и история — `docs/handoff.md`, ТЗ этапа 15 —
`docs/execution-stage-15.md`. Папка `docs/` в образ не попадает (`.dockerignore`).

## Локальный прогон

Docker на этой машине **не работает** (виртуализация выключена в BIOS).
Docker Desktop не запускать, профиль `test` из compose не поднимать.

**Интерпретатор гейта — Python 3.12, как в образе (`python:3.12-slim`), с 18.10.2026:**

```
C:\Users\vladz\venvs\trading_bot-312\Scripts\python.exe
```

venv вне OneDrive и репозитория, CPython 3.12.10 (`py install 3.12`), пакеты — `pip install -r requirements-dev.txt`
(те же пины, что в образе). pytest, smoke, mypy, ruff, стенд и сценарии — им. Смена `requirements*.txt` — тот же
`pip install` в этот venv до гейта. `python` в PATH — заглушка WindowsApps (exit 49), не использовать; 3.14
(`AppData\Local\Python\pythoncore-3.14-64`) — до 18.10 гейт шёл на нём, переход сверен: на `8636b4c` оба дали
pytest 2070 / 0 skipped, smoke ×2 exit 0, вывод mypy и ruff — строка в строку.

Тестовая БД — нативный PostgreSQL 18, служба `postgresql-x64-18`:

```
DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/trading_bot_test
```

База `trading_bot_test` и роль `test`/`test` созданы, миграции применены.
`.env` локально НЕ создавать — всё переменными окружения запуска.

Нужны: `DATABASE_URL`, `BOT_TOKEN`, `ENCRYPTION_KEY`,
`TRADING_EXECUTION_ENABLED=true`. Для `smoke_check.py` на Windows-консоли
ещё `PYTHONUTF8=1`. `ENCRYPTION_KEY` тестовой базы — свежий Fernet на прогон
(`Fernet.generate_key()`), нигде не хранится.

Из сессии Claude Code pytest запускать с `--capture=sys`: при захвате по fd у дочерних процессов
невалидный stdin, и 45 тестов `test_rehearse_migration_script.py` падают на `subprocess` (WinError 50/6) — это среда,
не код (10.10).

**Прогон зелёный только при нуле skipped.** Без `DATABASE_URL` молча
пропускается ~115 интеграционных тестов, и счёт врёт.

Ориентир на 12.10.2026 (код деплоя 6, A.4): 1643 passed, 0 skipped, 0 failed; smoke 102/102; mypy 60.

Число тестов в этом файле — ориентир на момент записи, а не факт. Перед
тем как называть его в плане или отчёте, прогонять пакет и брать свежую
цифру.

**Перед каждым push — ПОЛНЫЙ pytest (0 failed, 0 skipped), не только тесты,
затронутые правкой.** Урок 28.09, дважды за день: `376d8ad` запушен с двумя
падениями в `test_scanner_commit.py` (фейк контекста — `object()`, сканер начал
читать `context.atr`; гонялись только тесты сканера), утром `a5c1ad4` — со
сломанным smoke (см. «Скрипты»). Падения в «чужих» файлах ловит только полный
пакет.

**Исключение (владелец 18.10): коммиты только с документацией пушатся без полного pytest**, если последний коммит
с кодом прошёл гейт (pytest 0 failed / 0 skipped, exit 0). Документация — файлы в `docs/` и `*.md`.
Проверка перед push — список файлов от последнего коммита с зелёным гейтом:
`git diff --name-only <gated>..HEAD`, каждый путь — `docs/…` или `*.md`. Любой другой файл (код, тест, скрипт,
конфиг, `miniapp/`, `deploy/`) — полный гейт как обычно. В отчёте о push называть `<gated>` и его цифры.

Гейт перед коммитом — **код выхода pytest**, не текст: `pytest … | tail -1 && git commit`
проверяет код `tail` (всегда 0). 02.10 `d325250` так ушёл в push с 1 failed
(`test_callback_audit`: остаток пользователя от прогона на старом коде — telegram_id в тестах
детерминированный с 500000). Писать `pytest > out; echo $?` и коммитить только при 0.

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

`scripts/smoke_check.py` — 99 проверок, гоняет диспетчер. Единственный
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

Скрипты исследований сигналов (`replay_history`, `trend_research`, `signal_outcomes`)
удалены вместе с сигналами 02.10.2026 — в истории git (до `1892b70`).

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

`scripts/miniapp_dev.py` — локальный стенд фронта Mini App (ветка `miniapp`): API с подменённой авторизацией
и фейковой биржей + `miniapp/` с заголовками CSP как у Caddy, `http://127.0.0.1:8765/`.

```
python -m scripts.miniapp_dev [--port 8765]
```

- те же переменные, что у smoke; отказ (exit 2) при `BOT_TOKEN` не `0:…`, запуске под `/opt/trading_bot`,
  `BINGX_TRADING_MODE` не demo, базе не `trading_bot_test`; слушает только 127.0.0.1; в образ не попадает
- `telegram_id=434343` зарезервирован под стенд; выход по Ctrl+C его убирает, после жёсткого kill — остаётся
  до следующего запуска. Тесты его не трогают (у функций пользователя стенда — свой `telegram_id`; проверяет
  `test_tests_never_touch_stand_user`) — pytest можно гонять при запущенном стенде
- свечи стенда — блуждание, которое кончается на `last`, одно и то же для таймфрейма (график не прыгает на
  перезагрузке); цена стоит на месте
- проверка в браузере — `scripts/miniapp_browser_check.py` (при запущенном стенде, во второй консоли):
  ```
  MINIAPP_CHROME="C:/Program Files/Google/Chrome/Application/chrome.exe" python -m scripts.miniapp_browser_check [--url http://127.0.0.1:8765/] [--wait 8] [--symbol XRP-USDT]
  ```
  headless Chrome по DevTools-протоколу: console (warning/error), исключения, нарушения CSP (`Log` + слушатель
  `securitypolicyviolation`), HTTP ≥ 400 и сбои загрузки, видимые экраны и число графиков. Страница не
  догрузилась (нет видимого экрана, SDK Telegram или библиотеки графиков) — тоже замечание: SDK с telegram.org
  в headless иногда не успевает за `--wait` (16.10 — примерно раз из трёх) → повторить или `--wait 15`. Со
  свежим профилем монеты нет — открывается поиск, графика нет; **`--symbol` кладёт монету в localStorage до
  скриптов страницы → экран рынка, ровно один график обязателен** (проверка 2.3 — с ним; график позже `--wait` — состояние
  дочитывается до 15 с, не появился — замечание). Путь к Chrome — `--chrome` или
  `MINIAPP_CHROME` (в скрипте не зашит). Только 127.0.0.1 (страница и DevTools). Exit 0 — чисто, 1 — замечания
  списком, 2 — окружение не готово (нет Chrome, стенд не отвечает, URL не 127.0.0.1). **Каждый запуск — свежий
  временный профиль, удаляется после**: со старым профилем Chrome исполняет закэшированный ES-модуль (15.10 так
  видели «старые» предупреждения). В образ не попадает
- сценарии браузера — `--scenario <имя>|all` (`scripts/miniapp_scenarios.py`, при запущенном стенде; **стенд,
  Chrome и pytest не параллельно — память**): рынок 2.3 (поиск, опрос, ТФ/линия, тема, ширины и поворот, скрытая
  вкладка, уход/возврат ×5, пустые свечи, 503 при загрузке и в опросе, 401), свечи 1м 2.3а, SDK 2а (заблокирован,
  завис), форма 2.4 (потолок плеча, 2R, debounce, BLOCK, ввод, нет ключа, нативный MainButton), подтверждение 2.5
  (маркет до «Сделка #N», showConfirm, LIVE, обрыв /confirm → «Не дошло» → повтор, WARN, лимит и отмена,
  истечение, BLOCK, «Назад к форме»), «Позиции» П.1 (`pos_*`: пусто, хедж и лестница, NULL/LIVE не связываются, ссылка
  в чат, нет ключа, 503, 401, опрос/скрытая вкладка, карточка → рынок; `menu_native` — меню «…»), линии позиции П.2
  (`market_lines` — хедж на свечах и «линии», закрыта → сняты; `market_lines_none`; `confirm_lines` — после итога; позиции стенда —
  `POST /dev/positions?preset=…`, `Setup.preset`), дизайн Д (`theme` — страница и график в наборе colorScheme, светлая ↔ тёмная; `design_theme` — шапка/фон/нижняя полоса
  Telegram и нативные кнопки в цветах набора, разряды, цена моно; `design_fonts` — все начертания Golos/Plex Mono/Caveat загружены из
  `vendor/fonts`, body — Golos, `font/woff2`; `design_market`/`design_live` — шапка B и плашка счёта, детали, LIVE, 320 px;
  `design_font_swap` — подмена `--mono` не двигает рынок по вертикали, Plex запрошен между `/api/me` и `/api/market`). Ссылка
  «💼 Позиции» без SettingsButton — в деталях плашки (`#account-plate` → `#account-positions`); `design_font_404`/`design_font_hang`
  — Plex недоступен, рынок и цена показаны (`Setup.intercept_fonts`); `design_form` — квитанция и «Открыть лонг · N XRP» = строки
  ответа preview (объём подменён на «86482», `PreviewTap`), только разряды; `design_confirm`/`design_confirm_refused` — «ЗАПИСЬ
  № opening_id» (без него — без номера), строки = карточка сервера, «было → стало» из превью и карточки (`CardTap`), таймер
  «действует M:SS»; `confirm_double`/`confirm_double_old`/`confirm_double_burst` — 8.0: двойное нажатие = один `/confirm`; 6.0: второе
  нажатие через ≥ 400 мс = один, залп быстрее = 0 (экран на «Нажми ещё раз»); `design_positions`/`design_positions_bar` — PnL и полоска позиций по ответу
  сервера (`bar`, `pnl_sign`), без стопа — красная рамка; `sdk_real` — настоящий SDK
  telegram.org, sha256 сверяется с копией стенда (расхождение — `WARN:` строкой отчёта, не замечание).
  **SDK telegram.org на стенде — копия `scripts/stand_sdk/telegram-web-app.js` по умолчанию** (Fetch подставляет её вместо сети:
  сценарии, `--shots`, обычная проверка; `--real-sdk` у обычной проверки и `Setup(real_sdk=True)` — настоящий). Прод и `index.html`
  не меняются. Обновление копии — новый файл и sha256 в `tests/test_miniapp_browser_check.py`. **Для `confirm_*` стенд — с `EXEC_OPEN_DRY_RUN=false`** (биржа стенда
  фейковая; в сухом прогоне сценарий скажет об этом). Перед каждым сценарием — `POST /dev/reset` стенда (новая
  фейковая биржа, пользователь стенда заново; только в `miniapp_dev.py`). Каждый — свой свежий Chrome; сеть
  подменяется через `Fetch` (свой статус, правка ответа, обрыв, зависание, `exact` — путь целиком) — следы своих
  правил замечанием не считаются. SDK с telegram.org не загрузился (сеть) — сценарий прерывается с этим текстом,
  повторить; так же `wait_for`, если посреди сценария показан `screen-sdk`. Стенд вне Telegram — initData-заглушка в хеше (`#tgWebAppData=…`, `page_url`), без неё — экран «Открой Mini
  App из бота» (`no_init_data`, `Setup(init_data=False)`); `me_502` — прокси без api (502 без JSON). `all` (60) ≈ 7 мин; exit 0/1/2 как выше. Новый экран — новый сценарий там же
- снимки экранов — `--shots <папка> [--shot market|form|confirm|positions|all]` (`scripts/miniapp_shots.py`, при
  запущенном стенде; подэтап «Дизайн Mini App»): экраны 1–4 × светлая/тёмная (themeParams клиента в хеше +
  `prefers-color-scheme`, `Setup.theme`) × 390/320 px, страница целиком, масштаб 2, свои кнопки (браузер 6.0).
  Файл `<экран>-<тема>-<ширина>.png`; **папка внутри репозитория — отказ (exit 2)**: снимки «до» —
  `C:\Users\vladz\OneDrive\Desktop\trading\miniapp_shots\before\`, «после» — `…\after\`. Каждый экран × тема — свой
  Chrome, 16 снимков ≈ 2 мин; замечания — exit 1
- **мутации (правило владельца 18.10):** исходы CAUGHT / MISSED / **INCONCLUSIVE** — проверка не состоялась из-за сети или SDK
  telegram.org («SDK … не загрузился», exit 2). INCONCLUSIVE — не «пойман»: перепрогнать; итог прогона мутаций зелёный только при
  INCONCLUSIVE = 0, в отчёте — отдельной цифрой. **Сначала база:** каждая проверка спеки зелёная на чистом коде — иначе мутации
  не прогонять (18.10: «пойманы» на проверке, которая падала и без мутации)

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
   снапшот пересоздавали).
   С Mini App архив везёт и `miniapp/` (статика) и `deploy/Caddyfile`: в образ они не
   попадают (`.dockerignore`), Caddy монтирует их с диска — md5 сверять и для них
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
5. `docker compose up -d --build` — до Mini App. С сервисами `api`/`caddy`
   (подэтап 3): `docker compose build bot && docker compose up -d --no-deps bot api`.
   `api` — тот же образ `trading_bot-bot:latest`, своей сборки нет: без пересоздания
   он остаётся на старом образе. `caddy` пересоздаётся только при смене
   `deploy/Caddyfile` или его сервиса в compose (`up -d --no-deps caddy`); статика
   `miniapp/` читается с диска — достаточно `git archive`
6. `alembic upgrade head`
7. Показать `docker compose ps` и последние 50 строк логов

Откат кода: `docker tag trading_bot-bot:rollback_<ts> trading_bot-bot:latest
&& docker compose up -d --no-deps bot` (с Mini App — `--no-deps bot api`: один образ).
Статику `miniapp/` и `deploy/Caddyfile` откатывает снапшот кода шага 1. Удалять rollback-тег
(`docker rmi trading_bot-bot:rollback_<ts>`) — только после первого
«Пульс reconciler» без ошибок на новом образе (раз в час; сканера с 02.10.2026
нет) и с «да» владельца.

**Миграции на проде — только после явного «да» владельца.** Порядок:
свежий дамп → показать SQL офлайн-режимом alembic (без подключения
к базе) → дождаться «да» → применять. Если alembic применяет миграцию,
которой не ждали — остановиться и сказать.

### Деплой Mini App (первый: `api` + `caddy`, подэтап 3)

План — `docs/miniapp-plan.md` §6 и решения 17.10 в handoff. **Каждый шаг — по «да» владельца.**
Поддомен `app.postfactumtrade.ru` → `147.45.111.10` (A-запись в REG.RU; AAAA и CAA нет).

0. `master` догоняет `miniapp` fast-forward'ом — после сводки «что меняется в боте» и «да».
   Swap 2 ГБ (§6а) — до деплоя, своим «да» с командами
1. Чек-ап A–H, плюс только чтение: `getent hosts app.postfactumtrade.ru` = `147.45.111.10`,
   `ip -4 addr` содержит его; `ss -tlnp '( sport = :80 or sport = :443 )'` и
   `ss -ulnp 'sport = :443'` — пусто; `ufw status`, `iptables -S INPUT`, `iptables -S
   DOCKER-USER`, `nft list ruleset` — 80/443 не режутся; `curl -sI
   https://acme-v02.api.letsencrypt.org/directory` — 200; `sshd -T` (§6а); available ≥ 700 МБ
   с запасом api ~150 + caddy ~40. Фильтр хостера (панель Timeweb, 80/443) смотрит владелец
2. «Не нажимай кнопки, перезапускаю» → «ок» владельца
3. Шаги 1–4 обычного деплоя (umask, дамп, снапшот, архив с `miniapp/` и `deploy/`, md5,
   `rollback_<ts>` с `docker image inspect`); после распаковки `chmod -R go-w deploy miniapp`
4. Override — своим «да»: переменные исполнения bot и api — **одним якорем** `x-exec-env`
   (шаблон `deploy/override.example.yml`, значения — из текущего override прода), в якоре
   `EXEC_OPEN_DRY_RUN: "true"` для обоих; `MINIAPP_ALLOWED_TELEGRAM_IDS: ""` (403 всем) — только
   api; `MINIAPP_URL` — только bot. Права 600. Проверка: `docker compose config` — у bot и api
   одинаковые имена и значения EXEC_*/TRADING_* (печатать имена и «совпадает», не значения)
5. `docker compose build bot && docker compose up -d --no-deps bot` → логи, «бот запущен»
6. `docker compose up -d --no-deps api` → `docker compose ps` (api healthy), `logs/api.log`
   («API Mini App запущен», `allowed_ids: 0`), `docker stats --no-stream` (api ≤ 200 МБ)
7. `docker compose up -d --no-deps caddy` → `docker logs trading_bot_caddy` — выпуск
   сертификата. **Сбой HTTP-01 (или любой ошибки ACME) — сразу `docker compose stop caddy` и
   доклад, без повторов**: у Let's Encrypt лимит неудачных проверок; том `caddy_data` не удалять
8. Снаружи (с сервера по внешнему имени; с этой машины 80/443 перехватываются): `curl -sI
   https://app.postfactumtrade.ru/` — CSP/HSTS/nosniff/no-referrer как в `deploy/Caddyfile`,
   без `Server`; `/api/health` — 200; `/api/me` без initData — 401, с поддельной подписью — 401
9. Id владельца в `MINIAPP_ALLOWED_TELEGRAM_IDS` → `up -d --no-deps --force-recreate api`
10. `MINIAPP_URL=https://app.postfactumtrade.ru/` у bot → пересоздать bot (снова «не нажимай
    кнопки») → кнопка меню Mini App. BotFather — шаги владельцу (handoff). Проверка на iPhone
    (§8) — сухой прогон; живое открытие из Mini App (`EXEC_OPEN_DRY_RUN=false` в якоре,
    пересоздать bot и api) — отдельным «да»

Откат Mini App: `docker compose stop caddy api` — бот и чат не затронуты; кнопка меню —
`MINIAPP_URL` пусто в override + пересоздать bot (кнопка команд). Образ — общий откат выше
(`--no-deps bot api`). Тег отката снимать после пульса reconciler и «да».

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

**Перед любым пересозданием/рестартом бота** (деплой, включение и снятие `EXEC_OPEN_FAULT`, смена токена) — написать владельцу
«не нажимай кнопки, перезапускаю» и дождаться его «ок». 12.10 снятие сбоя совпало с «Да» на «Закрыть всё» (#40): SIGTERM между
коммитом PENDING-строки и POST — действие застряло в CARD, строка в PENDING. С B.9 (`919ae8ad384e`) такое доводит
восстановление (`app/execution/action_recovery.py`, при старте и каждым циклом reconciler, только чтение биржи), но правило «не
нажимай» в силе: итог приходит позже и может быть «сними лишний в BingX».

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
   нет (`health=none`) — это норма. С Mini App ещё `trading_bot_api` (health — `healthy`) и
   `trading_bot_caddy` (healthcheck нет)
2. `df -h / /opt`, `free -m`, `uptime`; `du -sh /opt/backups
   /opt/trading_bot/logs`; `docker system df`. Диск VPS — 28 ГБ
   (не 40), RAM ~1.9 ГБ, swap нет (после §6а — `swapon --show`: 2 ГБ, used). С Mini App —
   `docker stats --no-stream`: `api` ≤ 200 МБ, иначе флаг
3. `timedatectl` — `System clock synchronized: yes` (иначе подпись BingX
   начнёт отказывать)
4. `ss -tulpn` — 5432 и 6379 на хосте не слушают; `ufw status`. С Mini App наружу слушают
   только 22, 80, 443 (+10050 — агент Timeweb); 8000 на хосте нет. С 17.10 ufw **active**
   (deny incoming): 22/80/443 tcp всем (v4/v6), 10050/tcp — только `92.53.116.12`, `.111`,
   `.119` (= `Server=` в `/etc/zabbix/zabbix_agentd.conf`); другое правило или inactive — флаг.
   Опубликованные порты Docker идут мимо ufw (FORWARD) — главное по-прежнему пункт 4а.
   Проверка 10050 снаружи — не с этой машины (исходящие перехватываются: «открыт» любой
   порт); на сервере — `iptables -L ufw-user-input -v -n` и `UFW BLOCK` в `journalctl -k`
4а. `docker ps --format '{{.Names}} {{.Ports}}'` — порты публикует только `trading_bot_caddy`
   (80, 443/tcp); у db, redis, bot, api — пусто
5. `stat -c '%a %n' .env docker-compose.override.yml`;
   `find /opt/backups -maxdepth 1 -type f ! -perm 600` — и для `.tar.gz`
   из списка `tar tzf | grep -E '(^|/)\.env$'` (есть ли внутри `.env`)

### B. Код на проде

6. Локально: для каждого файла `git ls-tree -r --name-only <commit> app`
   посчитать `git -c core.autocrlf=false show <commit>:<path> | md5sum`,
   список передать по stdin в `md5sum -c --quiet -` в `/opt/trading_bot`.
   Плюс сравнить списки файлов (лишние файлы на проде). `<commit>` —
   последний задеплоенный, его время коммита сверить со `StartedAt`.
   С Mini App — так же для `miniapp`, `deploy` и `docker-compose.yml`
7. `docker compose exec -T bot alembic current` == `alembic heads`

### C. Конфиг

8. Из `get_settings()` печатать только: `trading_execution_enabled`,
   `exec_dry_run`, `exec_allow_live_mode_orders`, `bingx_trading_mode`,
   `bingx_base_url`, `bingx_demo_base_url`,
   `confirm_lock_ttl_seconds`, `exec_position_mode_ttl_seconds`,
   `exec_daily_digest_hour`, `log_json`, `environment`,
   `reconciler_notify_max_age_hours`, `reconciler_pulse_every` (с `c8306f9`/`90fae6e`),
   `ai_enabled`, `ai_market_summary_enabled` (с этапа 2; по умолчанию false),
   `position_monitor_price_seconds` / `position_monitor_snapshot_seconds` (с этапа 5; 15 / 60),
   `exec_open_dry_run` / `exec_open_allow_live` / `openings_interval_seconds` (открытие сделки
   из бота, M5; по умолчанию true / false / 15), `exec_open_fault` — **не пустой — флаг** (с B.9 ещё
   `interrupt_action_after_pending` / `interrupt_action_after_post` — обрыв действия этапа 4 на каждом «Да», только демо)
   (управляемый сбой включают только на время проверки аварийных веток на демо и сразу снимают;
   с деплоя 3 — ещё `exec_open_close_lock_wait_seconds` (8);
   при live бот с ним не стартует, в логе раз в час WARNING «EXEC_OPEN_FAULT включён»).
   С деплоя 5 — `bingx_rate_limit_threshold` (20) и `bingx_private_rate_limit_threshold` (2; в INFO
   «Троттлинг BingX…» поле `threshold`).
   `bingx_base_url` — только публичный клиент; ключевой клиент в режиме
   demo ходит на `bingx_demo_base_url`
8а. Override прода (`docker-compose.override.yml`, только имена и значения флагов, не
   секреты): `EXEC_OPEN_DRY_RUN` — на 08.10 был `"false"` (Т0); **на деплое Mini App у bot и
   api — `"true"`**, живое открытие из Mini App — отдельное «да». С Mini App: переменные
   исполнения — якорем `x-exec-env` на оба сервиса; если якоря нет — `docker compose config`:
   имена и значения EXEC_*/TRADING_* у bot и api совпадают (печатать «совпадает», не значения).
   Из настроек api — `miniapp_init_data_ttl_seconds` и **число** разрешённых id (сами id не
   печатать); `miniapp_url` у bot

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
12. Последние `Фоновый цикл завершён: daily_jobs` (сканер удалён 02.10.2026 —
    строк `setup_scanner` и «Цикл сканера завершён» на новом коде нет). Монитор
    позиций с этапа 5 — раз в 15 с, начало/конец цикла на DEBUG; на INFO —
    `Пульс монитора позиций: …` раз в час (последний не старше часа; `ошибок` —
    флаг, если не 0), `Уведомление о приближении` (`overshoot_pp` — перескок за
    порог; регулярно > 5 — повод для WebSocket), «Монитор позиций пропущен: идёт
    действие с позицией». Пропуск reconciler с этапа 5 — «Сверка пропущена: идёт
    действие с позицией (exec:lock)». С `27ac9db` рассылки
    daily_jobs пишут INFO `Рассылка отправлена: daily_report|daily_limit_reached|
    execution_digest`, сбой — WARNING `Рассылка не доставлена…` / `…выброшена`;
    на коде до него — только `user_settings.execution_digest_last_sent_date`
    (пункт 14). С `90fae6e` reconciler пишет `Пульс reconciler: …` раз в
    `reconciler_pulse_every` запусков (60 — раз в час): последний не старше часа,
    `ошибок` и `пропущено по локу` — флаг, если не 0. `Лимит BingX после POST`
    (с `e194bb7`) писал путь входа; с этапа 4 — живые действия с позициями
    (при `EXEC_DRY_RUN=true` строк нет)

12а. С Mini App: `logs/api.log` — счётчики по уровням, `Traceback`; `docker logs --since 24h
   trading_bot_caddy` — ошибки ACME/TLS. Срок сертификата: `echo | openssl s_client -connect
   app.postfactumtrade.ru:443 -servername app.postfactumtrade.ru 2>/dev/null | openssl x509
   -noout -enddate` — не меньше 30 дней (Caddy продлевает сам); `curl -sI` — заголовки как в
   `deploy/Caddyfile`, `Server` нет. `find /opt/trading_bot/deploy /opt/trading_bot/miniapp -perm /022`
   — пусто (нет записи для group/other; `git archive` кладёт 775/664 — после распаковки
   `chmod -R go-w deploy miniapp`, станут 755/644)

### E. База

Только агрегаты, через `Database(get_settings()).engine.connect()` и
первым запросом `SET TRANSACTION READ ONLY`, в конце `rollback()`.
`execution_orders.stage` хранится в нижнем регистре (`card`/`confirm`),
`status` — в верхнем.

13. `pg_size_pretty(pg_database_size(current_database()))`; соединения
    из `pg_stat_activity` по `state`
14. `execution_orders` с окна деплоя: `status × stage`. С 02.10.2026 входа
    по сигналу нет — до этапа 4 новых строк быть не должно, любая — флаг.
    Там же `user_settings.execution_digest_last_sent_date` — вчерашняя
    дата, если час `EXEC_DAILY_DIGEST_HOUR` по локальному времени прошёл.
    С миграции `7b4e2c9a1f35`: `reconciliation_events` с `notified_at IS NULL`
    — недоставленные уведомления (`gave_up_at` NULL — ещё в переотправке, не
    NULL — отказ); по `kind`, с `attempts` и возрастом
15. `signals` и `signal_notifications` — с 02.10.2026 не пишутся (сканер
    удалён, таблицы ждут отдельной миграции после дампа): новые строки после
    окна деплоя — флаг
16. `trades` со `status='OPEN'` по `source`; всего
    `source='SIGNAL_EXECUTION'`; из них `fill_confirmed = false`
    (предварительные, 15.5.4) — при сухом прогоне их быть не должно
16а. С миграции `0bf103d4a84d`: `execution_callbacks` с окна деплоя по
    `action`. С 02.10.2026 старые кнопки `exn:*` отвечают «Сигналы отключены»
    и ничего не пишут; с этапа 4 новые строки — только `pm_*` (кнопки действий с позицией,
    `position_action_id` не NULL, кроме `pm_open`). WARNING/ERROR «Нажатие кнопки действия не
    записано» — флаг: «Да» по нему отказало.
    Историческое (для строк до 02.10): `notification_id IS NULL` (битые кнопки) — флаг. Каждому
    `yes` — строка исхода в `execution_orders` по тому же
    `notification_id`; `yes` без исхода — только двойной тап под локом
    (пара `yes` по одному уведомлению за секунды). В логе — WARNING/ERROR
    «Нажатие кнопки исполнения не записано» — флаг: «Да» по нему отказало.
    `telegram_id` в логе — только маской `mask_telegram_id`
16б. С миграции `5951467e6d9a` (M1, этап 3): `position_actions` с окна деплоя по `kind × status`
    (с этапа 4); FAILED — флаг, каждому DONE/DRY_RUN — строки `execution_orders` с тем же
    `position_action_id`; `execution_orders` PENDING/UNKNOWN по действиям — флаг. В дайджесте —
    «Действия с позициями: карточек N (…)»; `trades.initial_stop_loss` по открытым сделкам с позицией на бирже — ставит
    reconciler при синхронизации уровней (INFO «Уровни сделки обновлены по бирже»);
    R считается от него (`Trade.risk_stop`)
16б′. С B.9 (M9 `919ae8ad384e`): `position_actions.account_mode` и `execution_orders.account_mode` — NOT NULL, до live
    только `DEMO` (другое — флаг). `position_actions` SUBMITTED старше пары минут — флаг (восстановление не довело: причина в
    INFO «Действие не восстановлено — повтор следующим проходом», поле `rows`); строки действий PENDING/UNKNOWN, CLOSE SUBMITTED
    или FILLED без цены старше пары минут — флаг. NOT_PLACED у строки действия — норма только после двух проверок: в логе INFO
    «Строка действия: ордера нет — первая проверка», потом «… вторая проверка, NOT_PLACED» (`since_first_s` ≥ 30). INFO «Действие
    восстановлено» (from → to) и `outgoing_messages.kind` `ACTION_RECOVERED` — восстановление сработало (после рестарта). Redis
    `action:notfound:*` — отметки первой проверки (TTL 1 ч); ключ старше пары минут при пустой очереди — флаг. Строка старше
    окна истории BingX (7 дней), не решённая одной проверкой по cid, — `execution_orders.error_code = 'MANUAL_RECONCILE'` (у
    активного действия — и `position_actions.error_code`), WARNING «… нужна ручная сверка»; из проходов исключена — **флаг:
    сверить с историей BingX и закрыть вручную** (по «да»). ERROR
    «Восстановление действия упало» / «Восстановление действий упало» — флаг. В восстановлении на биржу — только GET (в логе
    BingX нет POST/DELETE от него)
16в. С миграции `605ed85e8944` (M2, этап 5): `position_alerts` — по `kind`; строки только по
    уровням открытых позиций (исчезнувшие и перенесённые снимает монитор), строка без позиции
    на бирже старше пары минут — флаг. `execution_callbacks.raw_data` у новых нажатий не NULL.
    Строки постановки моста переноса (`client_order_id` `…SB`/`…TB`) — CANCELLED при снятом
    мосте; SUBMITTED — мост остался на бирже (карточка FAILED «не снялся»)

16г. С миграции `56733f24581b` (M5, открытие сделки из бота): `trade_openings` с окна деплоя по
    `status × entry_type`; ALARM — флаг (позиция без стопа), CONFIRMED/SUBMITTING/UNKNOWN/FILLED/
    PROTECTED старше пары минут — флаг (восстановление не довело), WORKING с `expires_at` в прошлом
    больше минуты — флаг (лимит не снят по сроку). Каждому DONE — сделка `source='BOT'` с
    `trade_id`; строки `execution_orders` с `trade_opening_id`: при `EXEC_OPEN_DRY_RUN=true` — только
    DRY_RUN. В логе: ERROR «Позиция открытия без стопа…», «Аварийное закрытие…», «Запасной стоп не
    встал» — флаг; INFO «Открытие восстановлено» — восстановление сработало (после рестарта)

16д. С деплоя 2 (A.1/B.1, M8 `fcf50d29396e`): `execution_callbacks` `to_close`/`to_close_yes`/`to_close_no` — нажатия
    «🔴 Закрыть маркетом» под ALARM (каждому `to_close_yes` — CLOSE-строка открытия). `outgoing_messages.kind`
    `OPEN_ALARM_<статус>` / `OPEN_WORKING_<статус>` — тревога / ⏳ исправлены при выходе; `edits` с `event`
    (`delete_skipped`, `markup_removed`). В логе: WARNING «Итоговое сообщение в переписке мастера — не удаляю» —
    флаг (до 4a так терялся итог); INFO «Быстрый повтор после тревоги» (attempt, status), WARNING «Быстрые повторы
    исчерпаны…» — флаг (тревога дольше 5 с); WARNING «⏳ лимита / Сообщение тревоги не исправлено» — сообщение
    удалено или старше 48 ч; ERROR «message is not modified» больше не ожидается (C.1)

16е. С деплоя 3 (без миграции): `trade_openings` EMERGENCY_CLOSED с `error_code='ENTRY_FILL_UNREAD'` — закрыто, сделка не
    записана (вход не дочитан); старше пары минут — флаг. PROTECTED с `avg_price IS NULL` — под стопом, вход не дочитан; старше
    пары минут — флаг. В логе: WARNING «Вход не подтверждён за N попыток» (причина — в INFO «Вход не подтверждён по cid» перед
    ней), WARNING «Позиция после входа по POST не видна», ERROR «…закрыта аварийно, вход не дочитан…» — флаг; INFO «Вход исполнен
    по ответу POST…», «Вход дочитан по cid», «Повтор после тревоги уступил кнопке «Закрыть»» — норма; WARNING ««Да, закрыть»: лок
    позиции не освободился», «Всплывашка «Уже идёт действие» не принята…» — флаг. Redis: `exec:close_wanted:*` живёт ≤ 12 с —
    дольше или TTL -1 — флаг

16ж. С деплоя 4 (без миграции): `execution_orders` `role=CLOSE` UNKNOWN или FILLED с `price IS NULL` старше пары циклов — флаг (добор не
    дочитал: причина в INFO «Строка закрытия не добрана по cid»); `position_actions` FAILED с `error_message` «Исполнение закрытия не
    подтверждено.» при FILLED-строке CLOSE — флаг. `trade_openings` EMERGENCY_CLOSED с `error_code='CLOSE_FILL_UNREAD'` старше пары минут
    — флаг. `reconciliation_events.kind` `CLOSED_BY_BOT` — норма (выход кнопки записала сверка). В логе: INFO «Закрытие исполнено по ответу
    POST», «Строка закрытия добрана по cid», «Действие подтверждено добором» — норма; WARNING «Закрытие не подтверждено за N попыток» без
    «исполнено по ответу POST» перед ним — флаг (POST не FILLED, исход неизвестен)

16з. С деплоя 6 (A.4 шаг цены, без миграции): WARNING «Уровень открытия не на шаге цены — округлён перед POST» и «Уровень
    действия не на шаге цены — округлён перед POST» — флаг (после карточки уровни уже на шаге; бывает только у строк до A.4 или
    при смене точности контракта). INFO «Уровни открытия округлены к шагу цены» — норма (ввод с лишним знаком). INFO reconciler
    «Уровни сделки обновлены по бирже» у сделки открытия сразу после входа — флаг (уровень ушёл не тот, что в базе)

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
23а. **Перед деплоем (любым, с рестартом бота):** `get_positions()` и `get_open_orders()` —
   0 позиций и 0 открытых ордеров на демо (печатать только числа). Не 0 — деплой не
   начинать, доклад владельцу

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
- BingX: **тип и цену стопа/тейка проверять только по openOrders.** Запрос ордера по orderId/cid
  (`trade/order`) для живого closePosition-ордера на демо отдаёт `type: LIMIT` и пустой `stopPrice`
  (Т3, 08.10) — по нему брать только статус
- BingX: **«ордера нет» = история (`allOrders`) + openOrders.** `allOrders` не возвращает живые (NEW)
  ордера (Т5, 08.10) — проверка только по истории пропустит стоящий ордер. Точечно — запрос по cid
  (109421 «order not exist»)
- `execution_orders.cancel_source` (с M7, очередь A.3): у строки `CANCELLED` — кто снял ордер:
  `EXCHANGE` (биржа сама, например при закрытии позиции), `BOT` (вытеснение, срок лимита, замена
  вложенного на closePosition), `USER` (кнопка владельца, в т.ч. снятие старого стопа/моста при
  переносе этапом 4). **NULL у `CANCELLED` = «на бирже точно не стоит (подтверждено openOrders),
  кто снял — неизвестно»** — не «не проверяли». **Умолчание — NULL (24.10); `EXCHANGE` только по
  доказательству** (`settle(exchange_ids=…)`): ордер стоял в openOrders перед нашим закрытием (под
  локом «Да», в проходе открытия) или его отмена в истории в допуске [T − 1 с, T + 10 с] от
  `updateTime` **нашего** закрывающего ордера (найден в выдаче по orderId из БД; нет — NULL).
  `allOrders` отбирает по **времени создания** ордера — окно истории от `min(created_at)` строк минус
  запас (`history_window`), не от закрытия; окно старше 7 дней или выдача в 500 — неполное, «нет в
  истории» по нему не доказано. SUBMITTED у ордера, которого на бирже нет, — ложь:
  её читают `settle` и Mini App. Закрывает строки стопа/тейка только `app/execution/settle.py` — и только по свежему
  openOrders (ордер там есть — строку не трогать), выбор по сделке/открытию/positionId, не по символу. Статус пишется только `CANCELLED` (две L); `CANCELED` — только у
  строк до M7, M7 их переводит
- **Шаг цены (A.4):** в POST — только цены на шаге (`10^-pricePrecision`, отдельного tick size BingX не отдаёт; не на шаге
  биржа молча округляет сама — Т1, 08.10). Округление — только `app/execution/price_step.py`. **Правило владельца (12.10):
  округление никогда не увеличивает риск в $ сверх выбранного:** если объём считается после — стоп дальше (открытие);
  если объём фиксирован (этап 4) — стоп ближе. Тейк — всегда ближе, лимит — в выгодную сторону. В базу пишется то, что ушло
  на биржу; перед каждым POST уровень не на шаге округляется тем же правилом, пишется в базу и даёт WARNING
- Молчаливый фолбэк вместо ошибки — баг. Если ожидаемого нет, падать
  внятно, а не подставлять первое попавшееся
- Новые настройки в `Settings` обязаны иметь дефолты
- `downgrade` в миграциях должен работать всегда
- **CHECK с `IN (...)` в downgrade — в хранимой форме Postgres:**
  `(col)::text = ANY (ARRAY[('x'::character varying)::text, …])`, как `_OLD_ACTIONS` в M5
  (`56733f24581b`) и `_M5_ACTIONS` в M8 (`fcf50d29396e`). `IN (...)` Postgres хранит как
  `(ARRAY[…])::text[]`, а при разборе заново (restore дампа в копию репетиции, смена типа колонки)
  выражение становится поэлементным и дальше не меняется. Через `IN` схема после downgrade ≠ baseline,
  и репетиция падает на сверке схемы: 08.10 на M5, 10.10 на M8 (урок не перенесли). В upgrade `IN`
  допустим, его сверяют с офлайн-SQL, а не с baseline. Точную строку брать из отчёта репетиции
  (строка `<` diff) или из `pg_get_constraintdef` восстановленной копии. Проверяет статически
  `tests/test_migration_check_downgrade.py` (исключение — M1: после CHECK меняется тип той же колонки)
