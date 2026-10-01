# Trading Journal Bot — передача в новый чат

Обновлено 01.10.2026: **прод — `ff72e02` (код `8273158`), миграция `0bf103d4a84d`: журнал нажатий и
`NON_POSITIVE_EQUITY`, задеплоено 01.10 14:22 UTC** (раздел «01.10, этап 15», читать первым). Задача
«репетиция миграции скриптом» — DONE. Предыдущий прод — `0798194` (код = `4c2e337`: строгий разбор
ответов BingX), раздел «29.09, вечер». Ещё раньше `972df0b` (INSUFFICIENT_MARGIN против свободной маржи, формат
уведомлений о закрытии) — раздел «29.09, день». Предыдущий деплой `42cc965` (живые формы read-back,
B4) — раздел «29.09», подраздел «Деплой 29.09 10:28 UTC». Ещё раньше `b90acfd` (ручной стоп/тейк на бирже →
«закрыто вне бота»; LINK #3 закрыта сверкой) — подраздел «Деплой 29.09 09:09 UTC». Утренний деплой
`e941a3a` (признаки сигнала, «Объём пробоя», отчёт исходов, миграция `ea93de72860d`) — там же и
«28.09, ночь» (что вошло). Предыдущий прод `eea28cc`
(уведомления «хотя бы один раз», пульс reconciler, троттлер по (метод, путь), миграция
`7b4e2c9a1f35`) — раздел «28.09, поздний вечер». Предыдущий прод `42efa3f` (блоки A/D/CROSSED/B/C, миграция
`19c5c0deedca`) — раздел «28.09, вечер». Деплой утра (`41a720f`, 15.6) — раздел 28.09 под
ним; раздел 26.09 — предыдущее состояние, его хвосты и уроки ещё действуют; всё
ниже — архив. Заменяет `handoff-2026-09-22.md` и `handoff-2026-09-26.md` (до 28.09 лежал вне
репозитория, в `trading/`). Читать вместе с `docs/architecture.md` (отстаёт) и `CLAUDE.md`
в корне `trading_bot`. ТЗ этапа 15 — `docs/execution-stage-15.md`.

## 01.10 — replay детекторов на истории: калибровка, гипотеза о плане не подтвердилась — стоп

**`63d7777` — `scripts/replay_history.py` + `tests/test_replay_history.py` (36). Тестов 1574
(0 skipped). Детекторы не тронуты; локально, публичные ручки BingX, прод не участвует.**

- Повод — разрез H1 отчёта исходов (прод, 01.10 ~14:40): H1 n=38, 4/27/7, R нетто −0.70,
  убыточен во всех срезах; H4 n=18, R нетто +0.92. Критерий владельца: фильтр для H1 (и H4 —
  тем же критерием) принимается, только если подобран на одной половине 12 месяцев и держится
  на второй; на живых 38 ничего не меняем
- Разведка 01.10 (по одному GET): klines H1 годичной давности есть (BTC), лимит 500 / 10 с.
  Funding — публичная `/openApi/swap/v2/quote/fundingRate`, форма `fundingRate`, `fundingTime`,
  `markPrice`, `symbol`; `startTime` ручка игнорирует (отдала последние 1000 — 02.11.2025…
  01.10.2026), глубже — страницами по `endTime`, не проверено; не дойдёт — «н/д»
- **Калибровка 01.10 — 48/56 = 86% (порог 80%), exit 0**: 1h 34/38, 4h 14/18; эталон — JSON
  `signal_outcomes` 01.10, окно [−3; +1] свечи, равенство стопа и тейка. 20 запросов klines.
  Не найдено 8: 6 — replay рядом с близкими, но не равными уровнями (формирующаяся свеча живого
  скана), 2 — рядом нет (ADA 4h SHORT 16.09, XRP 1h SHORT 30.09)
- Replay за всё окно дал 131 уведомление против 56: TTL-повторов 0, всё — «новый слот».
  ~~Гипотеза «символы добавлялись в план сканера 14–22.09»~~ **не подтвердилась**: план —
  `trading_plans.allowed_symbols` в БД (кнопки бота, не git), дат расширения нет ни в handoff,
  ни в git; `DEFAULT_ALLOWED_SYMBOLS` — те же 10 символов с первого коммита 10.09
- **Настоящая причина — эталон до 23.09 неполный.** `signal_notifications` создана миграцией
  `b0943282b974` (деплой 23.09 ~09:06 UTC) с бэкфиллом **по строке на слот `signals`** из его
  состояния на момент миграции. Слот один на (символ, ТФ, уровень) и перезаписывается: до 23.09
  в эталоне 17 строк = 17 различных (символ, ТФ), остальные READY потеряны; уровни у этих строк
  — от последнего скана слота, время — от последнего уведомления. «Первое живое уведомление»
  символа 14–22.09 — это его последний READY перед миграцией. **Касается и отчёта исходов
  `signal_outcomes`: до 23.09 он видит не все сигналы и не те уровни** (H1 до миграции — 10 из 38)
- Проверка без сети (кэш свечей), только после миграции: **1h — живых 28, replay 28, найдено
  26/28 (93%); 4h — 11, 11, 11/11 (100%)**. Все 4 ненайденных H4 и 2 из 4 H1 — строки бэкфилла,
  их уровней replay-детектор не давал ни на одной закрытой свече. 2 ненайденных после миграции
  (XRP 1h SHORT 30.09 18:21, BNB 1h SHORT 01.10 13:21) — таких уровней у replay тоже не было:
  живой скан видел незакрытую свечу (у BNB стоп тот же, тейк 756.5918 против 756.7742)
- Основной прогон (`run --months 12`) — **не запускался**: по правилу владельца гипотеза не
  подтвердилась → стоп, доклад

## 01.10, этап 15 — журнал нажатий и NON_POSITIVE_EQUITY (прод с 14:22 UTC)

**Прод: `ff72e02` (код `app/` = `8273158`), миграция `0bf103d4a84d`. Тестов 1538
(0 skipped), smoke 67/67 дважды (exit 0), mypy 68 (тот же набор), ruff 3833: F821 0, RUF100 4,
I001 10, E501 55 — прирост только RUF001–003 (кириллица).**

| Коммит | Что |
|---|---|
| `83c3bf0` | журнал нажатий `exn:open/yes/no` — таблица `execution_callbacks`, миграция `0bf103d4a84d` |
| `aca13f8` | `telegram_id` в логах `access.py`/`errors.py` — только маской |
| `8273158` | пустой счёт — отказ `NON_POSITIVE_EQUITY` вместо `INVALID_LEVELS` |

- **Журнал нажатий (префлайт 15.7, «до 15.7 обязательно»).** `execution_callbacks`: `user_id`
  (FK CASCADE), `action` (CHECK open/yes/no), `notification_id` (без FK, nullable), `chat_id`,
  `message_id`, `callback_query_id`, `created_at`. Пишет `app/execution/callback_audit.py`
  первым действием хендлера, своей сессией с немедленным коммитом (откат апдейта запись не
  забирает), `lock_timeout` 3s. «Да» — запись до Redis-лока (двойной тап тоже пишется); **сбой
  записи на «Да» — вход не выполняется** (ни лока, ни `execution_orders`), алерт «Не удалось
  записать подтверждение — вход не выполнен»; «Открыть»/«Нет» при сбое продолжают. Битые данные
  кнопки — `notification_id` NULL, сырые данные (до 64) в лог. INFO «Нажатие кнопки исполнения»
  с `tg` = `mask_telegram_id` (`88…918`); ERROR «…не записано» — без трейса и текста исключения
  (в нём параметры SQL, `chat_id` = `telegram_id`). Чек-ап — пункт 16а CLAUDE.md
- Проверено: Postgres не ждёт незакоммиченного пользователя — сразу FK-ошибка (тест);
  `lock_timeout` срабатывает при строке `users` под `FOR UPDATE` в чужой транзакции (тест).
  Фикстура `ctx` хендлерных тестов коммитит пользователя, как в проде
- **`NON_POSITIVE_EQUITY`**: в `evaluate()` сразу после баланса, до `AVAILABLE_MARGIN_UNKNOWN`;
  REFUSED с ценой, позиции не читаются. Текст «На счёте нет средств: equity 0 VST — объём входа
  не из чего считать. Пополни счёт и дождись следующего сигнала». `calculate_size` — та же
  проверка страховкой. Миграция не нужна
- Каждый новый тест падает на старом коде (хендлеры — 11 из 11, шаг 2 — 8 из 8)
- **Хвост, не чинить: дрейф `server_default` у `exchange_credentials.mode`.** `alembic check`
  на `trading_bot_test` видит `modify_default` для `exchange_credentials.mode` — модель и БД
  расходятся в серверном дефолте. Не от этой работы (было и до `0bf103d4a84d`), на поведение
  не влияет; разобрать отдельно, иначе следующая автогенерация миграции его подхватит
- **Репетиция на копии прода, 01.10 13:28 UTC (`e6a30ad`, лог `rehearsal_20261001_132801` удалён)
  — exit 0, ИТОГ: OK.** `--from ea93de72860d --checksum --expect-columns execution_callbacks.action
  --allow-count-change execution_callbacks`; free -m available 976–988 МБ, свежая сборка. 11
  шагов ✅: база 10 МБ → tmpfs 256; копия 172.19.0.2; baseline 15 таблиц, 325 объектов схемы,
  md5 по 15; upgrade/downgrade ×2 по одной строке Running; строки 15 таблиц = baseline на всех
  шагах (`execution_callbacks` — «—» / 0 ⚠ допущено); md5 всех 15 таблиц после обоих downgrade =
  baseline; схема после downgrade = baseline, после второго upgrade = первому. Diff baseline →
  up1 — 8 колонок, CHECK `action`, FK CASCADE, PK, 2 индекса — совпадает с офлайн-SQL. Строки
  прода на момент: execution_orders 54, signal_notifications 237, trades 7, trade_fills 12.
  После: `tb_*`, тега, дампов нет; `trading_bot` running, restarts 0, ревизия `ea93de72860d`
- **На проде открыты две демо-сделки бота** (01.10): AVAX #6 (11:21 UTC) и BNB #7 (12:21 UTC),
  `fill_confirmed`, 4 строки SUBMITTED — их SL/TP. Деплой с остановкой бота на время миграции —
  как при открытой LINK #3 (SL/TP на бирже, reconciler догонит после старта)
### Деплой 01.10 14:22 UTC — с миграцией (`ff72e02`, `0bf103d4a84d`)

- Префлайт: md5 `app/` хоста и контейнера = `0798194` (124); ревизия `ea93de72860d`
- Дамп/снапшот `pre_deploy_20261001_134816` (600, `gzip -t`, 15 таблиц, ревизия `ea93de72860d`,
  `.env` в снапшоте, `logs` нет). `git archive` `ff72e02` без `.env`/override: md5 всех 241 файла
  совпали, `.env`/override не тронуты (600, mtime прежние)
- Точка отката до build: `trading_bot-bot:rollback_20261001_134816` → `e88c7650bbc2` (= образ
  работающего контейнера, inspect). Build при работающем боте → `af734f72aec0`, `alembic heads`
  в нём `0bf103d4a84d`, `.sh` в образе нет. Офлайн-SQL из нового образа = локальный, «да» владельца
- Снимок до стопа (14:22:27, БД READ ONLY + GET positions/openOrders, демо-хост): AVAX #6 LONG
  10812, BNB #7 LONG 191.52 — объёмы и `positionId` = биржа; SL/TP #47/#48/#50/#51 SUBMITTED =
  openOrders NEW (те же orderId, стоп/тейк те же). Вход в журнале 11.006 / 769.96 против
  `avgPrice` 11.005 / 769.95 — известное округление
- Stop 14:22:46 → upgrade (ровно одна строка `Running upgrade ea93de72860d -> 0bf103d4a84d`) →
  current `0bf103d4a84d (head)` → up 14:22:53 UTC. **Простой 7 с**
- После: образ `af734f72aec0`, restarts 0; md5 `app/` контейнера = HEAD (126); `execution_callbacks`
  есть, 0 строк. Лог с up — 0 ERROR/WARNING/Traceback/Conflict. Первый цикл reconciler 14:24:03
  (INFO троттлера на `openOrders`); повторный снимок 14:24:51 — обе сделки = биржа, SL/TP живы,
  новых `reconciliation_events` нет, недоставленных 0, `trades.updated_at` прежние — за простой
  SL/TP не срабатывали
- Удалён `rollback_20260929_174951` (`c05b71f3e7de`, образ `972df0b`) по «да» владельца. Остаётся
  `rollback_20261001_134816` — **до следующего деплоя** (решение владельца)
- **Ждёт:** проверка журнала нажатий на живом сигнале — при следующей карточке владелец нажмёт
  «Открыть», показать строку `execution_callbacks`. Первый цикл сканера на новом образе — ~14:53 UTC,
  пульс reconciler — ~15:23 UTC (в чек-апе)

## 01.10 — репетиция миграции скриптом: DONE

`scripts/rehearse_migration.sh` — единственная процедура репетиции (CLAUDE.md «Репетиция
миграции», `--checksum` в команде всегда). Прод не менялся: `0798194`, `ea93de72860d`.

| Коммит | Что |
|---|---|
| `3fc30ab` | скрипт, тесты, `.gitattributes`, `.dockerignore`, shellcheck-py |
| `800fa18` | CLAUDE.md: процедура скриптом, откат `downgrade <rev>` |
| `66a1545` | фикс SQL (`ORDER BY 1 COLLATE`, `"char"`), тест всего SQL через psql, коды выхода 2/1 |
| `b41d8a9` | handoff: прогон 1 на `tb_fakeprod` (OK, OK, fault wrong-db → 1) |
| `72ef24d` | handoff: копия прода — OK, совпало с 29.09; пометка «не прогонялся» снята |
| `43c5aa9` | `--checksum`, `--allow-data-change`, `--expect-null` |
| `eea2a91` | handoff: `--checksum` на `tb_fakeprod` с данными — OK |
| `a85d3db` | `--fault data-change`, SQL только ASCII, тест порядка md5 |
| `540641b` | handoff: `--fault data-change` → 1, ❌ только signals |
| `53a520a` | handoff: копия прода с `--checksum` — OK |

Тестов 1506 (0 skipped). Подробности — два раздела ниже.

## 01.10, --checksum — md5 данных и --expect-null в репетиции

- `--checksum`: md5 строк каждой таблицы по колонкам снимка baseline (`ROW(…)::text`,
  строки по тексту, collation "C"; через `query_to_xml`, только SELECT) — в baseline и
  после каждого downgrade, расхождение — exit 1 со списком таблиц; в отчёте таблица md5
  (8 знаков, ❌). `--allow-data-change t.c` — колонка исключается из md5 (только с
  `--checksum`; колонки нет в baseline — отказ, exit 2). Таблица без колонок после
  исключения выпадает
- `--expect-null t.c`: после каждого upgrade `count(*) WHERE col IS NOT NULL` = 0, иначе 1
- SQL — шаблоны `checksum_sql`/`null_sql` с `@SPEC@`; тест гоняет их на `trading_bot_test`:
  md5 `alembic_version` = md5 Python от «(rev)», повторяемость, исключение колонки меняет
  md5 только своей таблицы, NULL-проверка `alembic_version.version_num` = 1
- Поддельный docker: OK с `--checksum --expect-null`; смена данных на downgrade → 1; она
  же с `--allow-data-change` → OK; неизвестная колонка в `--allow-data-change` → 2; не-NULL
  после upgrade → 1; без флагов — как раньше. Спецификация в SQL — `signals:id` (без
  исключённой `atr_old`)
- В git — `43c5aa9`; тестов 1492 (0 skipped), shellcheck 0, ruff E501 55
- **Прогон на `tb_fakeprod` с данными, 01.10 11:05 UTC (`43c5aa9`, лог `rehearsal_20261001_110556`)
  — exit 0, ИТОГ: OK.** Источник на `7b4e2c9a1f35`: users 2, signals 3 (detail с кириллицей,
  `|`, пустой строкой; NULL в nullable), signal_notifications 3. Флаги `--checksum
  --expect-columns signals.atr,signal_notifications.atr --expect-null` на все 12 новых
  колонок. 11 шагов ✅: baseline — 15 таблиц, 313 объектов схемы, md5 по 15 таблицам; оба
  upgrade — 12 колонок целиком NULL; оба downgrade — md5 всех 15 таблиц = baseline (signals
  `29cb9e24`, signal_notifications `1fe1dbf0`, users `84d8aa8f`, alembic_version `aef35e63`,
  пустые — `d41d8cd9`). Независимо: `aef35e63` = md5 «(7b4e2c9a1f35)», `d41d8cd9` = md5 ''.
  Строки и схема — как в прогоне 1 (diff — те же 12 колонок). Источник после — тот же
  (`7b4e2c9a1f35|2|3|3`), `tb_fakeprod`/сеть удалены, `tb_*`, тега, дампов, логов нет;
  `trading_bot` running, restarts 0
- SQL md5 (по запросу владельца показан): агрегация строк — `string_agg(r, chr(10) ORDER BY r
  COLLATE "C")`, колонки в `ROW` — `ORDER BY x COLLATE "C"`; ORDER BY был с `43c5aa9`. Тест
  (`a85d3db`): md5 многострочной таблицы из Python (строки `ROW(…)::text`, сортировка по байтам
  UTF-8) = md5 скрипта
- **`--fault data-change` (`a85d3db`)** — только с `--checksum`, `--source-container` не прода,
  без `--rewind-to`: после upgrade 1 в КОПИИ к первой text-колонке непустой таблицы (порядок
  снимка схемы, без `--allow-data-change`) « [fault]» в одной строке (DO-блок, ROW_COUNT ≠ 1 —
  исключение). Тест на `trading_bot_test` в транзакции с откатом: меняется md5 ровно целевой
  таблицы, после отката — как было; пустая таблица — исключение. SQL-шаблоны — только ASCII
  (кириллица в `RAISE` ломалась на Windows: argv psql — cp1251; тест на все шаблоны). Тестов
  1506 (0 skipped), shellcheck 0, ruff E501 55. Поддельный docker: fault → 1 с ❌ signals; fault
  при исключённой text-колонке → 1 «нет непустой таблицы с text-колонкой»; без fault — OK
- **Прогон `--checksum --fault data-change` на `tb_fakeprod` с данными, 01.10 11:38 UTC
  (`a85d3db`, лог `rehearsal_20261001_113809`) — exit 1 (ожидался):** шаги 1–7 ✅, шаг 8 FAULT
  `UPDATE signals.detail` ✅, шаг 9 downgrade 1 ❌ «данные (downgrade 1) ≠ baseline по md5:
  signals»; таблица md5 — ❌ только у signals (`77fc3bfb` → `26c980a3`), остальные 14 = baseline;
  строки и схема без расхождений. md5 signals/users в baseline этого прогона отличаются от
  baseline прогона 11:05 (`29cb9e24`/`84d8aa8f` → `77fc3bfb`/`868aa86e`) — это сравнение
  между двумя заливками источника (`created_at` = `now()` при вставке), не внутри прогона.
  Внутри каждого прогона md5 стабилен: 11:05 — baseline = down1 = down2 по всем 15
  таблицам; 11:38 — единственное расхождение signals от fault, users = baseline. Источник после — `7b4e2c9a1f35|2|3|3`, md5
  `signals.detail` до и после один (`e187dd35…`), строк с `[fault]` 0; `tb_fakeprod`/сеть
  удалены, `tb_*`, тега, дампов, логов нет; `trading_bot` running, restarts 0
- **Копия прода с `--checksum`, 01.10 11:39 UTC (`540641b`, лог `rehearsal_20261001_113908`) —
  exit 0, ИТОГ: OK.** `--from ea93de72860d --rewind-to 7b4e2c9a1f35 --checksum --expect-columns
  signals.atr,signal_notifications.atr --expect-null` на все 12 новых колонок; free -m available
  1012 МБ, свежая сборка. 12 шагов ✅: база 10 МБ → tmpfs 256; дамп 28K; копия 172.19.0.2;
  перемотка → `7b4e2c9a1f35`; baseline 15 таблиц, 313 объектов, md5 по 15 таблицам; оба upgrade
  — 12 колонок целиком NULL (на копии после перемотки — пустые, на проде с 29.09 в них есть
  значения); оба downgrade — md5 всех 15 таблиц = baseline (signals `708b3462`,
  signal_notifications `4b66e941`, trades `d97455fe`, execution_orders `c16d568d`, …). Строки
  (прод = все шаги): execution_orders 48, signal_notifications 231, signals 38, trades 6,
  trade_fills 11, mistake_types 13, reconciliation_events 4, strategies 3, trade_mistakes 2,
  остальные 0/1. Diff baseline → up1 — те же 12 колонок. После: `tb_*`, тега, дампов нет;
  `trading_bot` Up 42 hours, restarts 0, `alembic current` = `ea93de72860d (head)`; лог удалён

## 01.10 — `scripts/rehearse_migration.sh` (проверен на сервере, копия прода — OK)

**Прод не менялся: `0798194`, миграция `ea93de72860d`. Тестов 1484 (0 skipped; +48 —
`tests/test_rehearse_migration_script.py`), shellcheck 0.11 — 0 замечаний, ruff E501 55
(новый тест-файл — только RUF001).**

Закрывает хвост «процедура репетиции миграции — скриптом в репо» (раздел 28.09, поздний
вечер). Процедура, запуск и критерии приёмки — CLAUDE.md «Репетиция миграции» и «Скрипты».
Решения владельца 01.10: дамп в файл; второй downgrade по умолчанию; откат на проде —
`downgrade <rev>`, не `-1` (CLAUDE.md исправлен); данные пока только `count` — ~~md5 строк
(`--checksum` + `--allow-data-change table.col`) — отдельным коммитом позже~~ **сделано 01.10
(раздел «01.10, --checksum» ниже)**; count прода —
для информации; tmpfs = max(256 МБ, 3 × размер базы), `--memory 256m`, `fsync=off`,
`full_page_writes=off`; `.sh` исключены из образа.

Проверено локально (Docker здесь нет): pytest-файл (форма, обёртки `dk`/`al`, отказы
аргументов, `meta_py` на ревизиях репо, `guard_py` на `trading_bot_test`); прогон всего
скрипта через `bash -s` с поддельным `docker` (вне репо, scratchpad сессии) — OK-путь,
`--rewind-to`, `--no-final-downgrade`, `--fault wrong-db` (код 1, alembic не запускался),
неверный `--from` (2), потеря строки на downgrade (1; с `--allow-count-change` — OK),
падение upgrade (1), мало RAM (2), остаток дампа (2), `--to` не head (2), уборка не удалась
(3), SIGTERM посреди upgrade (1, уборка чистая). Настоящий docker/Postgres/alembic — нет.

**Дальше, каждый шаг — с «да» владельца, команды показать до запуска:**
1. ~~`pg_database_size` прода~~ **Снято 01.10 (только чтение):** 9 501 719 байт (9279 kB) →
   скрипт округляет до 10 МБ → tmpfs max(256, 30) = **256 МБ**, `--memory 256m` — без
   изменений. Оценка пика копии (прикидка, не замер): кластер initdb ~40 + база ~10 + WAL
   20–50 + тронутые shared_buffers ~20–40 + процессы ~20–30 ≈ **110–170 МБ из 256**. Если OOM —
   сначала `-c shared_buffers=32MB` копии, не `--memory`. `free -m`: total 1895, available
   1000, swap 0 — пороги скрипта (512) и сборки (700) проходят; пик прогона (копия ≤256 +
   alembic ~100–150) оставляет ~600 МБ
2. Прогон на одноразовом источнике (`--source-container tb_fakeprod`), затем
   `--fault wrong-db` на нём же; два прогона подряд без ручной уборки.
   **Первая попытка 01.10 ~09:22 UTC (`800fa18`) — exit 1 на шаге «Дамп»:** `count_sql`
   с `ORDER BY 1 COLLATE "C"` — Postgres понимает это как выражение (целое с collation),
   не номер столбца: `collations are not supported by type integer`. Уборка чистая, лог
   `/opt/backups/rehearsal_20261001_092204.log` — удалить после зелёного прогона;
   `tb_fakeprod` + `tb_fakeprod_net` (ревизия `7b4e2c9a1f35`) оставлены для повтора.
   Поддельный docker SQL не выполнял — дыра закрыта тестом: весь SQL скрипта через `psql`
   в READ ONLY на `trading_bot_test`. Он же нашёл второй баг, до которого прогон не
   дошёл: `schema_sql` — `text || "char"` (`pg_constraint.contype`), нужен `::text`.
   Заодно коды выхода сведены к одному правилу (CLAUDE.md «Скрипты»): 2 — отказ по
   проверке входа, 1 — любой сбой выполнения и провал проверок репетиции; сбой команды в
   предусловиях (список контейнеров, размер базы, образ) — теперь 1, не 2.
   Защита адреса и сверки после шагов — 1 (решение владельца 01.10).
   **Прогон 1 на `66a1545` (01.10 09:33–09:34 UTC) — пройден целиком**, каждый прогон —
   своя свежая сборка (free -m available 926–927 МБ):
   - A, `--source-container tb_fakeprod` (лог `rehearsal_20261001_093313`) — **exit 0, ИТОГ:
     OK**: 11 шагов ✅; база 9 МБ → tmpfs 256; адрес копии 172.20.0.2 перед каждым шагом;
     baseline 15 таблиц, 313 объектов схемы; upgrade/downgrade ×2 по одной строке
     Running, строки = baseline (источник пустой: 0, `alembic_version` 1), схема после
     downgrade = baseline, после второго upgrade = первому; diff baseline → up1 — ровно 12
     nullable-колонок `ea93de72860d` (по 6 в `signals` и `signal_notifications`)
   - A повтор без ручной уборки (лог `…093346`) — **exit 0, ИТОГ: OK**, те же 11 ✅
   - B, `--fault wrong-db` (лог `…093418`) — **exit 1** (ожидался): шаги 1–6 ✅, шаг 7
     ❌ `guard: db=postgres addr=172.20.0.2 MISMATCH — ожидалось db=trading_bot_rehearsal;
     upgrade не запускался`, строк Running нет, уборка ✅
   - после: `tb_fakeprod` до удаления — на `7b4e2c9a1f35` (источник не изменён);
     `tb_fakeprod`/`tb_fakeprod_net` удалены; `tb_*`, `trading_bot:rehearsal`, дампов —
     нет; логи 092204/093313/093346/093418 удалены
3. **Прогон на копии прода 01.10 09:38 UTC (`b41d8a9`, лог `rehearsal_20261001_093759`) —
   exit 0, ИТОГ: OK.** Аргументы `--from ea93de72860d --rewind-to 7b4e2c9a1f35
   --expect-columns signals.atr,signal_notifications.atr`; перед ним free -m available
   1022 МБ, свежая сборка. 12 шагов ✅: база 10 МБ → tmpfs 256; дамп 28K, 600, gzip -t;
   копия 172.19.0.2 (`--internal`); ревизия копии `ea93de72860d` = --from; перемотка →
   `7b4e2c9a1f35` (адрес ✅); baseline 15 таблиц / 313 объектов схемы; upgrade, downgrade,
   upgrade, downgrade — по одной строке Running, адрес ✅, строки = baseline, схема после
   downgrade = baseline, после второго upgrade = первому, обе ожидаемые колонки ✅.
   Строки (прод (инфо) = baseline = up1 = down1 = up2 = down2): ai_reports 0,
   alembic_version 1, exchange_credentials 1, execution_orders 45, mistake_types 13,
   reconciliation_events 4, signal_notifications 229, signals 38, strategies 3,
   trade_fills 10, trade_mistakes 2, trades 5, trading_plans 1, user_settings 1, users 1.
   Diff baseline → up1 — 12 `ADD`: `atr`, `breakout_at` (timestamptz),
   `breakout_volume_ratio`, `ema50_distance_atr`, `stop_pct`, `volume_ratio_last`
   (numeric(28,12)), все nullable, в `signals` и `signal_notifications`.
   **Сверка с ручной репетицией 29.09:** 12 колонок — совпало; число строк между шагами не
   менялось — совпало (абсолютные числа выросли с 29.09: signals 37→38, notifications
   180→229, trades 4→5, execution_orders 42→45 — прод живёт). «Новые колонки все NULL»
   29.09 смотрели руками — скрипт значения не сверяет (только count; md5 — позже).
   После: `tb_*`, `trading_bot:rehearsal`, дампов нет; `trading_bot` Up 40 hours,
   restarts 0, started 2026-09-29 17:51:11 UTC — прод не тронут; лог удалён
4. ~~После зелёного 3 — убрать из CLAUDE.md абзац «Скрипт ещё не прогонялся»~~ **Сделано
   01.10** — скрипт теперь единственная процедура репетиции; ручная — в истории git
   (`b74a887:CLAUDE.md`)

## 29.09, вечер — строгий разбор ответов BingX (прод, читать первым)

**Прод: `0798194` (код `app/` = `4c2e337`), миграция `ea93de72860d` (без изменений). Тестов 1436 (0 skipped), smoke 66/66 дважды (exit 0), mypy 68 (−1: ушла ошибка удалённого
`MarketDataService.get_funding_rate`, новых нет), ruff 3774 (F821 0, RUF100 4, I001 10, E501 55;
+45 к 3729 — только RUF001–003; UP047 от `_decimal_or` исправлен в `4c2e337`).**

`_to_decimal(None/"")` → 0 был молчаливым фолбэком по всем ответам биржи. Разведка 29.09 (таблица
мест и последствий — в чате, не в репозитории): ноль уходил в журнал (комиссия/profit выхода в
сверке, `account_balance_at_entry = 0` при импорте), в ордера (время openOrders → 1970, read-back
не находил свой стоп и ставил второй; минимумы контракта 0 — SIZE_TOO_SMALL выключен) и на экраны.

### Что вошло

- `87cd3c4` **живые формы публичных ручек** (`tests/fixtures/bingx_public_20260929.json`): ticker,
  klines v3, premiumIndex, contracts; live и demo хосты, по одному GET без ключей, остаток
  498-499. klines v3 — объект **без closeTime**; contracts 1239/1185, у всех есть точности и
  минимумы, status только 1/25, `tradeMinQuantity` — JSON-float
- `f918eef` **удалены поля и методы без потребителя**: `Ticker.volume_24h/price_change_percent`,
  `Position.margin`, `Fill.realized_pnl` (в живом allFillOrders `profit` нет), числа
  `OrderResult` (форма POST живьём не снята), `HistoryOrder.orig_qty/stop_price`,
  `AttachedTpSl.price/quantity`; методы `get_order`, `get_order_by_id`, `get_position_history`,
  `get_funding_rate` — с тестами на них. **Порядок изменён против плана** (было четвёртым):
  строгость раньше удаления уронила бы разбор живых allFillOrders/openOrders/allOrders
- `4359fe2` **строгий `_to_decimal`**: None/"" → `ExchangeResponseError`. Законная пустота —
  только `_decimal_or(value, field, empty)`: не-FILLED `avgPrice/commission/profit` в
  `get_order_fill` и allOrders → 0; openOrders `stopPrice ""` → None (`OpenOrder.stop_price:
  Decimal | None`, read-back и сверка сравнивают с None); `liquidationPrice`, `availableMargin` →
  None. allOrders FILLED — `avgPrice/executedQty/commission/profit` обязательны; openOrders —
  `orderId/symbol/side/positionSide/type/status/time/updateTime`, числа, `leverage` обязательны
- `daa536e` **баланс, позиции, allFillOrders, тикер, свечи, контракты**: `equity/usedMargin/
  unrealizedProfit` обязательны, форма `{"balance": {...}}` и дефолт `asset` убраны; позиции без
  дефолта плеча 1; исполнения без дефолтов LONG/BUY; тикер — пустой ответ ошибка; свечи — время
  обязательно, закрытие = открытие + ТФ; **битый контракт выпадает из списка с WARNING**
  («Контракты без обязательных полей пропущены»), `SymbolInfo.min_notional` без дефолта
- `00916d7` **сверка при неразборном allOrders**: ошибка ловится по сделке — журнал не тронут,
  WARNING «Сверка сделки: история ордеров не разобрана» и ошибка в пульсе каждый цикл, остальные
  сделки/позиции без сделки/входы сверяются дальше (раньше исключение обрывало пользователя).
  Сбой подряд > 30 мин — одно событие AMBIGUOUS `history:{trade_id}:unparsed` через обычную
  доставку; история разобралась — событие разрешено
- Тесты: каждый новый падает на предыдущем коммите (stash `app/`: 40 + 62 + 3); замки —
  NEW с пустыми commission/profit в allOrders, неактивный контракт (status 25), живые публичные
  формы (`TestPublicLiveForms`)

### Деплой 29.09 17:51 UTC — только код (`0798194`)

- До сборки: `alembic heads` в новом коде = `ea93de72860d`, файлов `alembic/` с `972df0b` не менялось
- Префлайт: md5 `app/` хоста и контейнера = `972df0b` (124, лишних файлов нет); PENDING/UNKNOWN/
  SUBMITTED 0; открытых сделок 0; открытых и недоставленных `reconciliation_events` 0;
  `exec:lock` 0; ревизия БД `ea93de72860d`; `exec_dry_run=False`, demo, live-ордера запрещены
- Дамп/снапшот `pre_deploy_20260929_174951` (600, `gzip -t`, 15 таблиц, ревизия `ea93de72860d`,
  `.env` в снапшоте, `logs` нет). `git archive` `0798194`: md5 всех 233 файлов совпали,
  `.env`/override не тронуты (600, mtime прежние)
- Точка отката `trading_bot-bot:rollback_20260929_174951` → `c05b71f3e7de` (= образ работающего
  контейнера, inspect)
- Образ `e88c7650bbc2`; up 17:51:11 UTC; `alembic upgrade head` — ни одной строки `Running
  upgrade`, current `ea93de72860d`
- После: md5 `app/` контейнера = HEAD (124), рестартов 0. За первый час (до 18:53) — 45 строк, все
  INFO: 0 WARNING, 0 ERROR, 0 Traceback, 0 Conflict; «Контракты без обязательных полей пропущены» 0,
  «история ордеров не разобрана» 0
- Изнутри контейнера `get_symbols()` нового кода (по одному GET contracts без ключей, остаток
  499/498): активных контрактов live 1224, demo 1136; все 10 символов `EXEC_SYMBOL_WHITELIST`
  (ADA, AVAX, BNB, BTC, DOGE, ETH, GRAMTON, LINK, SOL, XRP) есть на обоих хостах, точности и
  минимумы заполнены. Различия demo/live: ETH `quantityPrecision` 3/2 и `tradeMinQuantity`
  0.001/0.01, GRAMTON `tradeMinQuantity` 1.351/1.344
- Первый цикл сканера 18:21:32 UTC — 10 символов, 30 запросов, 6.56 с; второй 18:51:32; пульс
  reconciler 18:51:26 — циклов 60 за 60 мин, ошибок 0, по локу 0, событий 0
- Удалён предыдущий `rollback_20260929_120528` (`fcacee6c28b4`) по «да» владельца. Остаётся
  `rollback_20260929_174951`
- Два SSH-таймаута подключения ~18:25 UTC при опросе логов раз в минуту; сервер при этом работал
  (uptime 26 дней, бот без рестартов) — сеть, не прод. Длинная сессия `docker compose logs -f`
  рвётся сервером («closed by remote host») — опрашивать короткими подключениями

### Хвосты

- ~~**equity `"0.0000"` (законный ноль, пустой счёт) → отказ `INVALID_LEVELS` «Баланс должен быть
  положительным»**~~ **Закрыто в коде `8273158`** (`NON_POSITIVE_EQUITY`, раздел «01.10, этап 15»),
  задеплоено 01.10 14:22 UTC
- **HistoryOrder: строгость только для FILLED.** `PARTIALLY_FILLED`/`CANCELLED` с
  `executedQty > 0` сверка выходами не считает (`reconciler.py` берёт только FILLED) — проверить
  на реальном счёте, бывают ли такие закрытия и какая у них форма
- Отсчёт 30 минут сбоя истории — в памяти процесса, как пульс: рестарт бота начинает его заново
- Поправка к разведке: импорт с пустой ценой/объёмом исполнения на старом коде в журнал не
  попадал — его останавливал CHECK БД (`ck_trade_fills_fill_price_positive`/`_quantity_positive`),
  импорт падал необработанным `IntegrityError`. Пустая комиссия записывалась нулём
- Строковые поля `_parse_order` (ответ POST) по-прежнему `or ""` — форма POST живьём не снята;
  вызывающие берут только `order_id` (пустой — WARNING и сверка по clientOrderID) и `raw`
- На проде продолжать смотреть WARNING «Контракты без обязательных полей пропущены» и
  «история ордеров не разобрана» в чек-апах (первый час — 0)

## 29.09, день — прод `972df0b`

**Прод: `972df0b`, миграция `ea93de72860d` (без изменений). Тестов 1327 (0 skipped), smoke 66/66
дважды (exit 0), mypy 69 (тот же набор), ruff 3729 (F821 0, RUF100 4, I001 10, E501 55, RUF059 18,
F401 7; +59 к 3670 — только RUF001–003). Открытых сделок в журнале нет.**

### Что вошло

- `765e678` **INSUFFICIENT_MARGIN против свободной маржи** (хвост 26.09). `calculate_size`: риск
  по-прежнему от equity (`account_balance`), а маржа входа + комиссия входа (`fee_rate × нотионал`,
  вверх до 1e-8) — против `available_margin`; `GuardInputs`/`ExecutionQuote` несут её отдельно.
  Текст отказа «Нужна маржа X + комиссия входа F, свободно Y». Карточка: «маржа X из свободных
  Y VST» (без комиссии)
- `BingXClient.get_balance`: `available` только из `availableMargin`; нет поля или `""` — `None`
  (раньше молча брали `balance`). `Balance.available: Decimal | None`; экран `/balance` при
  `None` — «Свободно: —»
- `evaluate`: `available` `None`, `< 0`, или 0 при `used_margin == 0` и equity > 0 — отказ
  **`AVAILABLE_MARGIN_UNKNOWN`** сразу после баланса (строка наблюдения с ценой, позиции не
  читаются); 0 при занятой марже — `INSUFFICIENT_MARGIN`. Фолбэка на equity нет.
  `frozenMargin` повторно не вычитаем, запаса на дрейф нет (решение 29.09)
- Проверено при реализации: код отказа пишется в `execution_orders.error_code` `String(64)` —
  миграция не нужна. **`_to_decimal(None)` и `_to_decimal("")` в `bingx.py` возвращают 0, а не
  ошибку** — для `availableMargin` обойдено явной проверкой; другие поля баланса (`equity`,
  `usedMargin`) при отсутствии по-прежнему молча станут 0 — хвост ниже
- VST-фикстура `test_balance_demo_mode_picks_vst_not_usdt` — из `13d3c7a` (11.09) без пометки
  живого, помечена СИНТЕТИКОЙ. Новые тесты баланса — синтетика из живого дампа
  `test_balance_list_of_assets_shape`
- `972df0b` **единый формат уведомлений о закрытии**: `Reconciler._close_text` для всех видов
  (стоп/тейк бота, ручной маркет/лимит, ручной стоп/тейк, частичное) — «Закрыта: DD.MM HH:MM»
  (`app/core/timefmt.py`), `fmt_price`/`fmt_qty`, комиссии `fmt_amount` без «+», PnL `fmt_money`.
  `CLOSED_OUTSIDE_BOT` — ℹ️ вместо ⚠️, причина в уведомлении короткая «Стоп/Тейк, изменённый
  вручную» (маркет/лимит — без строки причины); `Trade.exit_reason` в журнале полный. Частичное
  закрытие — ⚠️ остаётся. Старые `notify_text` в БД (переотправка) — в прежнем формате
- Тесты: каждый новый падает на старом коде (проверено откатом `app/` в stash: 16 из 16 для
  маржи, 8 из 8 для текста); замки — `test_lock_margin_plus_entry_fee_exactly_available_passes`,
  `test_lock_risk_base_is_equity_not_available`. Тесты sizing на старом коде падают на новом
  аргументе — поведение ловят тесты сервиса (`test_execution_service.py`, в т.ч. end-to-end на
  настоящем `BingXClient` без `availableMargin`)

### Деплой 29.09 12:06 UTC — только код (`972df0b`)

- До сборки: `alembic heads` в новом коде = `ea93de72860d`, файлов `alembic/` с `42cc965` не менялось
- Префлайт: md5 `app/` хоста и контейнера = `42cc965` (124, лишних файлов нет); PENDING/UNKNOWN/
  SUBMITTED 0; открытых сделок 0; открытых `reconciliation_events` 0; `exec:lock` 0 (и повторно
  перед up); ревизия БД `ea93de72860d`
- Дамп/снапшот `pre_deploy_20260929_120528` (600, `gzip -t`, 15 таблиц, ревизия `ea93de72860d`,
  `.env` в снапшоте, `logs` нет). `git archive` `972df0b`: md5 всех 232 файлов совпали,
  `.env`/override не тронуты (600, mtime прежние)
- Точка отката `trading_bot-bot:rollback_20260929_120528` → `fcacee6c28b4` (= образ работающего
  контейнера, inspect)
- Образ `c05b71f3e7de`, `alembic heads` в нём `ea93de72860d`; up 12:06:54 UTC; `alembic upgrade
  head` — ни одной строки `Running upgrade`, current `ea93de72860d`
- После: md5 `app/` контейнера = HEAD (124), рестартов 0; с up — 0 ERROR, 0 Traceback, 0 Conflict,
  4 WARNING `matplotlib findfont` (известное). Первый цикл сканера 12:37:15 UTC — 10 символов,
  31 запрос, 7.3 с; второй 13:07:14; пульс reconciler 13:07:09 — циклов 60 за 60 мин, ошибок 0,
  по локу 0, событий 0
- `rollback_20260929_085330` удалён ещё 29.09 утром; удалён предыдущий
  `rollback_20260929_102705` (`294023d66fb4`) по «да» владельца. Остаётся
  `rollback_20260929_120528`

### Хвосты

- **Живой `/user/balance` на ближайшем демо-входе** вместе с `/user/positions` (п. ниже в
  «29.09»): при открытой позиции снять `balance`, `equity`, `availableMargin`, `usedMargin`,
  `frozenMargin`, `unrealizedProfit` — заменить синтетику в тестах баланса, проверить, что
  `availableMargin` < equity при занятой марже. Только GET
- ~~**`_to_decimal` читает `None`/`""` как 0**~~ — закрыт вечером 29.09 (`4359fe2`, `daa536e`),
  см. «29.09, вечер»
- Граничный POST (резервирует ли BingX комиссию закрытия) не делали — решение 29.09

## 29.09 — утро (прод до 12:06 — `42cc965`)

**Прод был: `42cc965` (push — `e31550a` + handoff), миграция `ea93de72860d`. Тестов 1304 (0 skipped,
0 xfail), smoke 66/66 дважды (exit 0), mypy 69 (тот же набор, что на `94e20bc`), ruff 3670 (F821 0,
RUF100 4, I001 10, E501 55, RUF059 18, F401 7; +64 к 3606 — только RUF001–003). Открытых сделок в
журнале нет.**

### Деплой 29.09 10:28 UTC — только код (`42cc965`)

- До сборки: `alembic heads` в новом коде = `ea93de72860d`, файлов миграций с `b90acfd` не менялось
- Префлайт: md5 `app/` хоста и контейнера = `b90acfd` (124, лишних файлов нет); PENDING/UNKNOWN/
  SUBMITTED 0; открытых сделок 0; открытых `reconciliation_events` 0; `exec:lock` 0 (и повторно
  перед up)
- Точка отката `trading_bot-bot:rollback_20260929_102705` → `294023d66fb4` (= образ работающего
  контейнера, inspect). Дамп/снапшот `pre_deploy_20260929_102705` (600, `gzip -t`, 15 таблиц,
  ревизия `ea93de72860d`, `.env` в снапшоте, `logs` нет). `git archive` `42cc965`: md5 всех 230
  файлов совпали, `.env`/override не тронуты (600)
- Образ `fcacee6c28b4`, `alembic heads` в нём `ea93de72860d`; up 10:28:32 UTC; `alembic upgrade head`
  — ни одной строки `Running upgrade`, current `ea93de72860d`
- После: md5 `app/` контейнера = HEAD (124), рестартов 0; с up — 0 ERROR, 0 Traceback, 0 Conflict,
  4 WARNING `matplotlib findfont` (известное). Первый цикл сканера 10:58:53 UTC — 10 символов, 30
  запросов, 7.3 с; position_monitor/daily_jobs чистые; пульс reconciler 11:28:46 — циклов 60 за
  60 мин, ошибок 0, по локу 0, событий 0
- `rollback_20260929_085330` (`6a33993e53bc`) удалён по «да» владельца; остаётся
  `rollback_20260929_102705`

### Живые формы read-back и фикстуры (29.09, `076595a`…`42cc965`, задеплоено 10:28)

Разведка (только чтение, с «да» владельца):
- SELECT на проде: `execution_orders` #37–#42 — `client_order_id` `tj209u1E/S/T`, `tj215u1E/S/T`
  (буква роли заглавная), а биржа в GET отдаёт `tj209u1e`. Поиск GET по `clientOrderID` от
  регистра не зависит (read-back #37/#40 нашёл входы FILLED)
- Один GET на демо по несуществующему `clientOrderID`: **`code 109421`, `msg "order not exist"`**
  (остаток лимита `/trade/order` 29). 109414 в тестах был выдумкой — убран

Коммиты:
- `076595a` **Р2**: у не исполненного ордера живьём `commission ""`, `avgPrice "0.000"` —
  `_parse_order_fill` смотрит status первым; не FILLED — «не исполнен», FILLED с пустой
  комиссией — ReadbackIncomplete. Read-back ждёт FILLED повторами (раньше обрывался на первом
  NEW), вход, так и не ставший FILLED, дальше не отдаётся (не «исполнение 0»); ветка reconciler
  «найден в статусе NEW» стала достижимой (раньше — молчаливый повтор каждый цикл)
- `f58f60f` **Р1**: сравнение cid — casefold (`find_our_conditional`, `_is_own_order`). На живом
  `tj209u1e` старый код не ошибался; формат генерации не менялся
- `bb4cb92` **п.7**: исполнение без `tradeId`/`orderId` — `external_id` None + WARNING, дедуп
  только по непустому
- `59fb4ef` **п.8**: префиксы `entry:{id}:`, `qty:{id}:`, `stop_missing:{id}:` с двоеточием —
  вход 1 больше не разрешает расхождения входа 12 (у `stop_missing` был тот же дефект)
- `63f9141` фикстуры: пометки «СИНТЕТИКА ДО 15.5.5» сняты, тесты read-back/журнала — на живом
  LINK #3; чего живьём нет — синтетика из живого, помечена в тесте
- `da550d5` `_confirm_entry` B1–B8 и ветки 552/570/580/590; поведение «нет сделки → вечное
  AMBIGUOUS», «CANCELLED/CLOSED → только факт» записано в docstring (решение 29.09)

Хвосты:
- ~~B4~~ **закрыто `42cc965`:** OPEN-сделка без ENTRY-исполнения — не пересчитываем и не
  подтверждаем, AMBIGUOUS `entry:{id}:no_entry_fill` «Сделка #N без исполнения входа в журнале —
  подтверждение не выполнено, проверь вручную». Вход при этом FILLED и в следующие циклы не
  попадает — расхождение открыто, пока не разберёт владелец (как `no_trade`)
- **Снять на ближайшем реальном входе (только GET, п.4):** `/user/positions` по своей позиции
  с `liquidationPrice`, `initialMargin`, `margin` в allowlist — сейчас ключи есть, значений нет
  (разведка 27.09), проверки ликвидации read-back тестируются только синтетикой. Позиций на
  29.09 нет
- Живьём не сняты и остаются синтетикой: ответ POST условного ордера (спасение стопа), статус
  частичного исполнения, ручной условник в openOrders до срабатывания
- Живые формы, которые стоит помнить: allOrders округляет время до ближайшей секунды
  (`updateTime` 32.828 → 33.000), GET/openOrders — мс; `price` маркет-входа в GET — ориентир
  (14.398), в allOrders — `"0"`; `cumQuote` округлён до целого (29345 при 29344.32); один и тот
  же условник в openOrders — `reduceOnly true`, `closePosition "false"`, `positionID` позиции,
  в GET — `false`, `""`, `0`

### Деплой 29.09 09:09 UTC — только код (`e22d19c` + `b90acfd`)

- Префлайт: md5 `app/` хоста и контейнера = `e941a3a` (123); alembic `ea93de72860d`;
  PENDING/UNKNOWN 0; SUBMITTED — #38/#39 (по БД); `exec:lock` 0; открыта в журнале LINK #3
- Точка отката `trading_bot-bot:rollback_20260929_085330` → `6a33993e53bc` (образ `e941a3a`,
  inspect). Дамп/снапшот `pre_deploy_20260929_085330` (600, `gzip -t`, 15 таблиц, ревизия
  `ea93de72860d`, `.env` в снапшоте, `logs` нет). `git archive` `b90acfd`: md5 всех 231
  файлов совпали. Образ `294023d66fb4`, `alembic heads` = `ea93de72860d` (миграций нет)
- «Да» владельца на up (reconciler закроет #3) → up 09:09:42 UTC
- Первый цикл reconciler 09:10:53 UTC: **#3 CLOSED** — exit 14.776, объём 2037.8, fees
  29.727847 (14.672461 + 15.055386), pnl 736.484953 = (14.776 − 14.4) × 2037.8 − 29.727847,
  exit_reason «Стоп, изменённый вручную — закрыто вне бота», closed_at 04:03:24 UTC, выход
  `external_fill_id` `2104784078754553856`, `exchange_realized_pnl` 765.8499; #38/#39
  CANCELED; AMBIGUOUS #2 resolved 09:10:53; новое #3 `CLOSED_OUTSIDE_BOT`
  `close:3:2104784078754553856`, notified 09:10:53 (одно уведомление); PNL_MISMATCH нет
- После: md5 `app/` контейнера = HEAD (124), рестартов 0, 0 ERROR/WARNING/Traceback/Conflict
- `rollback_20260929_072615` (`24702baadaa8`) удалён по «да» владельца; остаётся
  `rollback_20260929_085330`
- **Хвост, косметика, не срочно — текст `CLOSED_OUTSIDE_BOT`:** «закрыто вне бота» дублируется
  в заголовке («⚠️ … закрыта на бирже вне бота») и в строке причины. Сделать: в уведомлении
  причину сократить до «Стоп, изменённый вручную» / «Тейк, изменённый вручную»; эмодзи
  заголовка закрытия вне бота ⚠️ → ℹ️. **`exit_reason` в журнале оставить полным**: карточка
  сделки и отчёт по демо-периоду показывают его без заголовка уведомления, и «закрыто вне
  бота» — главный факт; тексты сравниваются точным совпадением (`BOT_EXIT_REASONS`, подсчёт
  закрытий), а #3 уже записана полным — сокращение потребовало бы переписать историю
  (миграция данных) ради косметики. Сокращать — только отображение в уведомлении

### Деплой 29.09 07:39 UTC

- Префлайт: md5 `app/` хоста и контейнера = `eea28cc` (123, списки совпали); PENDING/UNKNOWN 0;
  SUBMITTED — #38/#39 (SL/TP LINK, **только по БД** — на бирже CANCELLED, см. ниже);
  `exec:lock` 0; открыта в журнале LINK #3
- Точка отката `trading_bot-bot:rollback_20260929_072615` → `24702baadaa8` (образ `eea28cc`,
  inspect). `rollback_20260928_162113` (`9aff2d1ad222`) удалён после проверок по «да» владельца
- Дамп/снапшот `pre_deploy_20260929_072615` (600, `gzip -t`, 15 таблиц, ревизия
  `7b4e2c9a1f35`, `.env` в снапшоте, `logs` нет). `git archive` `e941a3a`: md5 всех 228 файлов
  совпали; образ `6a33993e53bc`; `ls /app/scripts/signal_outcomes.py` в новом образе — есть
- Репетиция на копии прода (`postgres:16-alpine`, tmpfs, `172.19.0.2` проверялся перед каждым
  шагом): upgrade → downgrade → upgrade → downgrade, строк signals 37 / notifications 180 /
  trades 4 / execution_orders 42 не менялись, новые колонки — все NULL. Копия, сеть, тег, дамп
  удалены
- Офлайн-SQL — 12 `ADD COLUMN`, «да» владельца. Stop → upgrade (одна строка `Running upgrade
  7b4e2c9a1f35 -> ea93de72860d`) → current `ea93de72860d (head)` → up 07:39:54 UTC
- После: md5 `app/` контейнера = HEAD (123), рестартов 0, 0 ERROR/WARNING/Traceback/Conflict;
  первый цикл сканера 08:10:10 UTC — 10 символов, 31 запрос, 7.9 с

### Отчёт исходов сигналов (прод, 29.09 ~08:20 UTC, один прогон)

42 READY, горизонт 50, признаки все восстановлены (replay совпал 36/42, 85%); открытые вне
среднего (7, из них 3 — горизонт не пройден). R брутто / нетто (taker 0.0005):

| срез | n | тейк/стоп/откр | R брутто | R нетто |
|---|---|---|---|---|
| все | 42 | 11/24/7 | +0.06 | −0.03 |
| 1h | 27 | 4/19/4 | −0.47 | −0.57 |
| 4h | 15 | 7/5/3 | +1.06 | +1.01 |
| пробой | 39 | 10/22/7 | +0.05 | −0.04 |
| откат к EMA50 | 3 | 1/2/0 | +0.20 | +0.11 |
| LONG | 37 | 11/19/7 | +0.24 | +0.16 |
| SHORT | 5 | 0/5/0 | −1.00 | −1.16 |
| объём пробоя <1.3 | 19 | 4/13/2 | −0.09 | −0.18 |
| объём пробоя ≥1.3 | 19 | 6/8/5 | +0.29 | +0.21 |
| стоп <1% от входа | 11 | 1/10/0 | −0.70 | −0.87 |
| ATR 0.5–1% | 8 | 0/7/1 | −1.00 | −1.16 |
| один на пробой, 1h | 19 | 4/12/3 | −0.23 | −0.34 |
| один на пробой, 4h | 9 | 3/4/2 | +0.74 | +0.68 |

Выборки малые; к решению префлайта 15.7 (H4 и/или сниженный риск).

### LINK #3: закрыта ручным стопом на бирже, журнал — OPEN (AMBIGUOUS)

Разведка 29.09, только GET (allOrders, positions, openOrders, GET order по id — по одному):
- Владелец вручную перенёс стоп в безубыток. Дочерний ордер `2104784078754553856`:
  `STOP_MARKET` SELL LONG, stopPrice 14.800, avgPrice 14.776, executedQty 2037.8, commission
  −15.055386, profit 765.8499, `triggerOrderId` `2104641198668472320`, `workingType`
  `CONTRACT_PRICE`, reduceOnly, clientOrderId пуст, FILLED 2026-09-29 04:03:24 UTC
- Наши `…705` (STOP 13.526) и `…704` (TP 16.263) — **CANCELLED в 04:03:24 UTC**, в ту же
  секунду, что закрытие; в БД — SUBMITTED
- **Родительского условника `2104641198668472320` в allOrders нет; GET `trade/order` по его
  orderId возвращает дочерний ордер** (orderId `…553856`, `positionID` 0 — в allOrders у того
  же ордера positionID реальный). Время постановки и исходный стоп ручного условника из API
  не получить
- Позиции LINK нет, openOrders LINK пусто
- `reconciliation_events` по trade 3 — одна строка: #2 AMBIGUOUS
  `ambiguous:3:2104784078754553856`, создано 04:04:10, доставлено 04:04:11, не разрешено.
  STOP_MISSING по LINK не было
- Случай падает в `_classify_exit` (`app/execution/reconciler.py:114-124`) → AMBIGUOUS в
  `decide_trade` (`:164-175`); проверка стопа `stop_missing` (`:241-263`) принимает любой
  закрывающий `STOP_MARKET` на позиции — ручной стоп тревоги не давал
- Сырые ответы — в scratchpad сессии 29.09 (`link_manual_stop_*_raw.json`); при реализации —
  в `tests/fixtures/`
- **#3 в журнале не трогать** до решения по плану (распознать не наш закрывающий условник)
- **Решение — `e22d19c` (задеплоено 29.09 09:09, #3 закрыта сверкой — см. выше):** закрывающий reduceOnly-ордер с чужим
  `triggerOrderId` — `STOP_MARKET`/`STOP` → «Стоп, изменённый вручную — закрыто вне бота»,
  `TAKE_PROFIT_MARKET`/`TAKE_PROFIT` → «Тейк, …», вид `CLOSED_OUTSIDE_BOT`; трейлинг и прочее
  — AMBIGUOUS. `stop_missing`: защита — `STOP_MARKET` и `STOP`, трейлинг — нет. Фикстура —
  `tests/fixtures/bingx_demo_20260929_link_manual_stop.json`. **После деплоя reconciler на
  первом цикле закроет #3 сам**: 14.776 × 2037.8, комиссии 29.727847, PnL 736.484953 =
  (14.776 − 14.4) × 2037.8 − 29.727847, closed_at 04:03:24 UTC; #38/#39 → CANCELED; AMBIGUOUS
  #2 → resolved; одно уведомление
- **Хвост: `max_retries=0` у `BingXClient._request`** — ноль попыток, запрос не уходит,
  `AssertionError` (`bingx.py:503`). Никто так не зовёт; разведке — `max_retries=1`. Не чинить
  без решения
- ~~**Хвост: формат уведомлений о закрытии.**~~ **Закрыто `972df0b`** (раздел «29.09, день»). С `e22d19c` только `CLOSED_OUTSIDE_BOT` пишет
  «Закрыта: DD.MM HH:MM» (время исполнения в поясе пользователя, `app/core/timefmt.py`) и
  суммы через `fmt_price`/`fmt_qty`/`fmt_amount`/`fmt_money`. Стоп/тейк бота и частичное
  закрытие — по-старому (`fmt_decimal`, без времени) — перевести на тот же хелпер

## 28.09, ночь — признаки сигнала и отчёт исходов (задеплоено 29.09)

**В git: `876b055`…`306fef9` + docs, миграция `ea93de72860d`. Прод — `eea28cc`. Тестов 1251
(0 skipped), smoke 66/66 дважды (exit 0), mypy 69 (тот же набор), ruff 3585: F821 0,
RUF100 4, I001 10, E501 55 — как до работы; +109 — только RUF001–003 (кириллица).**

Повод — разовый разбор 28.09 (`retro_breakout`, вне репо): пробой с ретестом по READY-
уведомлениям — H1 −0.38R n=24, H4 +0.36R n=14, всего −0.11R n=38, до комиссий.

- `876b055` — миграция `ea93de72860d`: в `signals` и `signal_notifications` nullable
  `atr`, `volume_ratio_last`, `stop_pct`, `breakout_volume_ratio`, `breakout_at`,
  `ema50_distance_atr`; `snapshot_of` копирует. Офлайн-SQL — 12 `ADD COLUMN`, без
  бэкфилла. Репетиция на `trading_bot_test`: upgrade → downgrade → upgrade дважды, колонок
  12 → 0 → 12. **Репетиция на копии прода — ещё нет** (процедура в CLAUDE.md)
- `3ae0cb7` — детектор: мёртвый блок объёма в `_find_broken_level` убран; «Объём пробоя»
  (объём пробойной свечи / среднее за 20, порог 1.3) — **информационное** условие только на
  готовом READY, не фильтр. В WAIT-ветки не попадает: движок выбирает WAIT по числу
  выполненных условий, `classify_signal` отличает FORMING по единственному невыполненному.
  Замки: `tests/test_setups_lock.py` (эталон по 34 сценариям × пробой/откат/движок снят на
  `876b055`, `tests/fixtures/setups_lock_golden.json`; хэши `build_fingerprint`)
- `376d8ad` — сканер пишет признаки на READY (FORMING — NULL); `render_detail` READY —
  строка «Объём пробоя ×N (порог 1.3)» (только пробой); карточка анализа READY — все
  условия, ✅ / ⚠️ информационное невыполненное. **Fingerprint не менялся** — после деплоя
  активные READY заново не придут, старые кнопки не станут «устаревшими»
- `2d89656` — `scripts/signal_outcomes.py`, см. CLAUDE.md «Скрипты». Сборка
  `MarketContext` вынесена в `engine.context_from_candles` (прогон = код сканера)
- `306fef9` — фейк контекста в `test_scanner_commit.py` (`object()` без
  `atr`) — в `376d8ad` пропущен, ловился только полным пакетом

После деплоя:
- Строки READY до миграции — признаки NULL; отчёт восстанавливает их прогоном детектора
  на закрытых свечах до `notified_at` и помечает `replay` / `replay≠`. Формирующуюся свечу
  скана задним числом не восстановить, поэтому `replay≠` ожидаем часто — доля совпадений в
  шапке отчёта
- **Прогон `signal_outcomes` на проде — отдельным разрешением владельца**, после деплоя
- **Не проверено фактом: попадает ли `scripts/signal_outcomes.py` в образ.** Docker на
  этой машине не собирается. Косвенно — да: `Dockerfile` `COPY . .`, в `.dockerignore`
  `scripts/` нет, `scripts.check_redis` на проде запускается так же. Проверить при деплое:
  `docker compose exec -T bot ls /app/scripts/signal_outcomes.py`
- **Хвост: `trading_bot_test` копит READY-снимки тестовых пользователей** (с 14.09,
  BTC-USDT 4h, entry_low 100, у каждого свой `telegram_id` ~1029718+) — какой-то тест не
  убирает за собой. Найти тест (по `notified_at` 2026-09-14 12:50 и форме слота) и
  дописать уборку; мусор удалить. На счёт тестов пока не влияет, но
  `test_load_rows_runs_read_only_select` читает эти строки

## 28.09, поздний вечер — текущее состояние

**Прод: `eea28cc`, миграция `7b4e2c9a1f35`. Тестов 1189 (0 skipped), smoke 66/66 дважды
(exit 0), mypy 69 (тот же набор, что до работы), ruff 3476: F821 0, RUF100 4, I001 10,
E501 55 — как эталон 20aaf29 (3366); +110 — только RUF001–003 (кириллица).**

### Деплой 28.09 ~16:39 UTC

- Проверки до деплоя: все вызовы `send_notification`/`deliver_event` — без приведения
  `Delivery` к bool (`is Delivery.*` / `.final`); `27ac9db` без хунков `c8306f9`, pytest на
  `27ac9db` в отдельном worktree — 1163 passed
- Префлайт: md5 `app/` прода и контейнера = `42efa3f` (122, списки совпали);
  PENDING/UNKNOWN 0 (SUBMITTED — только живые SL/TP LINK, строки 38/39); `exec:lock` 0;
  открыта только LINK #3
- Точка отката до build: `trading_bot-bot:rollback_20260928_162113` → `9aff2d1ad222`
  (образ `42efa3f`, `docker image inspect`). `rollback_20260928_112859` (`e8c5db2c2e98`)
  удалён по «да» владельца
- Дамп/снапшот `pre_deploy_20260928_162113` (600, `gzip -t`, 15 таблиц, ревизия
  `19c5c0deedca`, `.env` в снапшоте, `logs` нет). `git archive` `eea28cc`: md5 всех 223
  файлов совпали; образ `24702baadaa8`
- Репетиция на копии прода (`postgres:16-alpine`, tmpfs, своя сеть, адрес `172.19.0.2`
  проверялся перед каждым шагом): upgrade → downgrade → upgrade → downgrade; счётчики строк,
  событие #1 и LINK #3 не менялись; после upgrade у события #1 `attempts` 0, `notify_text`
  NULL, `notified_at` стоит. Копия, сеть, тег и дамп удалены
- Офлайн-SQL из нового образа — 4 `ADD COLUMN` + индекс, показан, «да» владельца
- Stop → upgrade (одна строка `Running upgrade 19c5c0deedca -> 7b4e2c9a1f35`) → current
  `7b4e2c9a1f35 (head)` → up 16:39:01 UTC
- После: `confirm_lock_ttl_seconds` 187, `reconciler_notify_max_age_hours` 24,
  `reconciler_pulse_every` 60, `exec_dry_run` False, demo; 0 ERROR/WARNING/Traceback,
  0 рестартов; md5 `app/` в контейнере = `eea28cc` (123); LINK #3 без изменений (OPEN,
  fees 14.672461, одно исполнение, строки 37/38/39 FILLED/SUBMITTED/SUBMITTED); событие #1
  не переотправлено (`attempts` 0, `last_attempt_at` NULL), недоставленных 0. Циклы
  reconciler идут: 16:40:55–16:42:10 UTC сканов `reconciliation_events` +5 (выборка на
  переотправку), событие #1 не тронуто. Первая строка `Пульс reconciler` — ~17:39 UTC

Повод — чек-ап 28.09 (только чтение): 23.09 15:13 UTC дневная сводка потерялась
(`daily.py` ставил дату до отправки, результат не проверялся); тревоги read-back уходили
`message.answer` после правки карточки — упала правка, тревоги нет; правка «проверяю
исполнение…» стояла между отправкой ордера и read-back — сбой Telegram на ней обрывал
проверку/спасение стопа и запись сделки; reconciler не переотправлял `notified_at IS NULL`;
пульса не было.

| Коммит | Что |
|---|---|
| `e194bb7` | троттлер по (метод, путь); INFO `Лимит BingX после POST`; TTL лока 187 (4 с сна троттлера в формуле) |
| `27ac9db` | `Delivery` (DELIVERED/FAILED/FORBIDDEN) вместо bool; daily_jobs и монитор TP/SL — отметка после успеха, повтор, «выброшена» после полуночи, INFO на отправку |
| `c8306f9` | миграция `7b4e2c9a1f35`; переотправка reconciler (30 мин каждый цикл → раз в 10 мин → отказ в 24 ч), «⏱ … доставлено с опозданием», строка в «Аномалиях» |
| `03f4a03` | тревоги read-back — событиями, до карточки; правки пути «Да» не обрывают путь; итог без карточки — новым сообщением |
| `90fae6e` | пульс reconciler: INFO раз в 60 запусков, «Сверка: циклов N, последний HH:MM, ошибок E» в сводке |

- **Миграция `7b4e2c9a1f35`**: `reconciliation_events` + `notify_text` TEXT NULL,
  `attempts` INT NOT NULL DEFAULT 0, `last_attempt_at`, `gave_up_at` TIMESTAMPTZ NULL,
  частичный индекс `ix_reconciliation_events_undelivered`. Без бэкфилла. downgrade: виды
  тревог read-back → `AMBIGUOUS`, колонки/индекс удаляются. Офлайн-SQL показан; репетиция
  upgrade → downgrade → upgrade — на тестовой БД (с событием нового вида: downgrade дал
  AMBIGUOUS), затем на копии прода перед деплоем (выше)
- **Отступления от плана**: четвёртый вид тревоги `STOP_UNVERIFIED` («СТОП НЕ
  ПОДТВЕРЖДЁН», openOrders не прочитан) — тот же канал `result.alarm`, без него тревога
  терялась бы; в downgrade добавлен. INFO на отправку рассылок daily_jobs — в `27ac9db`, не
  в коммите пульса
- **При сбое Telegram теперь**: рассылки daily_jobs — повтор каждые 15 мин до местной
  полуночи; тревоги read-back и события reconciler — событие в БД, переотправка
  reconciler; FORBIDDEN — окончательно, один WARNING; итоговая карточка — новым сообщением
- **После деплоя проверить**: `confirm_lock_ttl_seconds` = 187; в логе через час —
  `Пульс reconciler`; в 21:00 Екб — `Рассылка отправлена: execution_digest` и строка
  «Сверка:» в сводке; `reconciliation_events` без `notified_at IS NULL`; на первом входе —
  строки `Лимит BingX после POST` (лимит POST `trade/leverage` не снят — записать сюда)
- STOP_MISSING reconciler после STOP_RESCUE_FAILED — оба уведомления, решение владельца
- ~~**Хвост: процедура репетиции миграции — скриптом в репо `scripts/rehearse_migration.sh`**~~
  **В git 01.10 (раздел «01.10»), на сервере ещё не прогонялся.**
  (`</dev/null` у каждой docker-команды, проверка адреса БД перед каждым шагом,
  `trap`-уборка). Урок `</dev/null` нарушен дважды за 28.09 при ручной сборке скрипта:
  утром — `pg_dump`, вечером — `docker run -i … alembic` съел остаток скрипта после первого
  upgrade (копию добивали отдельным скриптом, прод не задет)

### Находки чек-апа 28.09, не чиненные (только чтение)

- `.env` в 35 снапшотах кода `/opt/backups/*.tar.gz` и `env.20260908003342.bak` —
  старый токен в них недействителен, остальные секреты актуальны; снапшоты не ротируются
- LINK #3 (открыта): `external_position_id` и `external_fill_id` входа NULL — открыта до
  `c1880cf`, бэкфилла нет; позиция на бирже `2104122757805514754`
- мёртвый блок `if …: pass` — сейчас `setups.py:361-367`: фильтр объёма пробоя не
  работает. Ретро на 38 READY «Пробой с ретестом»: по последней свече прошли бы 13, по
  пробойной — 18 (порог 1.3)
- `import_history_days`, `import_poll_interval_seconds` — в Settings и в `.env` прода, код
  не читает; `AnnotateTradeStates` не используется
- docker-логи без `max-size`, journald 376 МБ, build cache 479 МБ к освобождению
- `logs/bot.log` **ротируется** (`RotatingFileHandler` 10 МБ × 5), ~120 КБ/сутки —
  прежний хвост «не ротируется» был ошибкой

## 28.09, вечер — прод `42efa3f`

**Прод: `42efa3f`, миграция `19c5c0deedca`. Тестов 1147 (0 skipped), smoke 66/66, mypy 69.**
Код — `4d7d8bc` (блоки), `904597d` (заглушка `get_margin_type` в smoke_check), `cec2421`
(чистка тестов), `42efa3f` (урок про smoke, замок `test_lock_pnl_not_checked_without_1r`).

### Деплой 28.09 ~11:37 UTC

- Префлайт: md5 `app/` прода = `41a720f` (121, списки файлов совпали); ENTRY в
  PENDING/UNKNOWN/SUBMITTED — 0 (два SUBMITTED — живые SL/TP LINK #3, строки 38/39);
  строк моложе 5 мин нет; `exec:lock` 0; открыта только LINK #3
- `GET /trade/marginType` (демо) по всем 10 символам whitelist — **все ISOLATED**
- Точка отката до build: `trading_bot-bot:rollback_20260928_112859` → `e8c5db2c2e98`
  (образ `41a720f`, проверено `docker image inspect`). `rollback_20260928_044423`
  (`bde832cb5714`) удалён по «да» владельца
- Дамп/снапшот `pre_deploy_20260928_112910` (600, `gzip -t`, дамп полный, `.env` в
  снапшоте, `logs` нет). `git archive` `42efa3f`: md5 всех 218 файлов на сервере совпали;
  образ `9aff2d1ad222`
- Репетиция `19c5c0deedca` на копии прода (`postgres:16-alpine`, tmpfs, своя сеть,
  адрес проверялся перед каждым шагом): upgrade → 4 колонки nullable, пусто →
  downgrade → колонок нет → upgrade; счётчики строк и LINK #3 не менялись. Копия,
  сеть и дамп репетиции удалены
- Офлайн-SQL из нового образа — 4 `ADD COLUMN`, показан, «да» владельца
- Stop → upgrade (одна строка `Running upgrade df411b3ca043 -> 19c5c0deedca`) → current
  `19c5c0deedca (head)` → up 11:37:55 UTC
- После: Settings изнутри — `exec_taker_fee_rate` 0.0005, `exec_liq_buffer` 1.5,
  `exec_maint_margin_rate` 0.008, `confirm_lock_ttl_seconds` 183,
  `exec_margin_type_ttl_seconds` 300, `exec_dry_run` False, `bingx_trading_mode` demo;
  0 ERROR/Traceback, 0 рестартов; md5 `app/` в контейнере = `42efa3f` (122);
  `@postfactumTrade_bot` polling. LINK #3 без изменений (OPEN, fees 14.672461, одно
  исполнение ENTRY, строки 37/38/39 FILLED/SUBMITTED/SUBMITTED), новых событий сверки 0
- **Первый цикл reconciler подтверждён только косвенно** (время после 11:39:04, нет
  ошибок и «упал», LINK не тронута): цикл без действий на INFO не пишет ничего — хвост
  «пульс reconciler» ниже в силе

| Коммит | Блок | Что |
|---|---|---|
| `f25c5e7` | A | taker-комиссия (`EXEC_TAKER_FEE_RATE` 0.0005) в RR гварда `INVALID_LEVELS` и в объёме; карточка «RR 1:X · с комиссией 1:Y» |
| `5463e50` | D | плечо от стопа (`EXEC_LIQ_BUFFER` 1.5, `EXEC_MAINT_MARGIN_RATE` 0.008), «Плечо Nx (план до Mx)»; проверка `liquidationPrice` в read-back; TTL лока **183 с** (16 вызовов) |
| `a5c1ad4` | CROSSED | `GET /trade/marginType` на карточке, кэш 300 с по символу; отказы `MARGIN_NOT_ISOLATED` / `MARGIN_MODE_UNKNOWN`; на «Да» — из `ExecutionQuote`, запросов не прибавилось |
| `2fad4c7` | B | `TargetSource` (LEVEL / FORMULA_2R) у обоих детекторов, «Цель: уровень X» / «Цель: 2R по формуле — X» в уведомлении; fingerprint не меняется — замок `test_fingerprint_ignores_target_source` |
| `4d7d8bc` | C + миграция | `19c5c0deedca`: `signals.target_source`, `signal_notifications.target_source`, `trade_fills.exchange_realized_pnl`, `execution_orders.risk_reward_net`; сверка PnL при полном закрытии → аномалия `PNL_MISMATCH` (> 0.01R); сводка «Средний RR: X · с комиссией Y» |

- **Миграция `19c5c0deedca`** — четыре `ADD COLUMN … NULL`, без бэкфилла и CHECK
  (офлайн-SQL снят с `df411b3ca043:19c5c0deedca --sql`). downgrade: `PNL_MISMATCH` →
  `AMBIGUOUS` (старый код падает на незнакомом kind), колонки удаляются. Репетиция на
  тестовой БД: upgrade → downgrade (событие-метка стало `AMBIGUOUS`, колонок нет) →
  upgrade. **Репетиция на копии прода — ещё не делалась**
- На SOL #4 сверка PnL дала бы разницу 0.24 при допуске 17.67 — не аномалия
- Детекторы и `MIN_RISK_REWARD` = 2 комиссию **не** учитывают (решение владельца)
- После деплоя в чек-апе: `confirm_lock_ttl_seconds` = 183, `exec_margin_type_ttl_seconds`
  = 300; старые уведомления строки «Цель» на карточке не показывают — так задумано

### Префлайт 15.7 (копится)

- **Сверить taker-ставку боевого счёта** с `EXEC_TAKER_FEE_RATE` (0.0005 снята на демо)
- Режим маржи боевого счёта по символам whitelist — `ISOLATED` (иначе карточка
  откажет `MARGIN_NOT_ISOLATED`)
- Хвосты с пометкой «до 15.7» ниже: лог callback исполнения, пределы POST на боевом
  хосте, `EXEC_SYMBOL_WHITELIST` сузить до одного
- **Решить: исполнение только H4 и/или сниженный риск** — по отчёту исходов сигналов
  (`scripts/signal_outcomes.py`; 28.09: H1 −0.38R n=24, H4 +0.36R n=14, всего −0.11R
  n=38, до комиссий; прод 29.09, R от RR снимка: H1 −0.57R нетто n=27, H4 +1.01R нетто
  n=15, всего −0.03R нетто n=42 — раздел «29.09»)
- ~~LINK #3: журнал OPEN при закрытой на бирже позиции (ручной стоп, AMBIGUOUS #2)~~ —
  **закрыта сверкой 29.09 09:10:53 UTC** (`b90acfd`, раздел «29.09»)

### Урок 28.09

- **Блок, добавляющий запрос к бирже, прогоняет smoke в том же коммите.** В `a5c1ad4`
  (CROSSED) карточка начала читать `marginType`, заглушки в `smoke_check.py` не было —
  smoke падал на запрете сети («✗ СЕТЬ», exit 1), коммит был запушен; нашлось только
  на финальной проверке, чинил отдельным `904597d`. pytest этого не ловит: фейки
  тестов — свои, `smoke_check` патчит методы `BingXClient` поштучно
- Отрицательные тесты, которые на старом коде падают только из-за нового типа, —
  замки, так и называются: `test_lock_pnl_not_checked_without_1r`,
  `test_fingerprint_ignores_target_source` (докстринг «ЗАМОК»)

### Хвост «сразу после» блоков 28.09

- ~~**`INSUFFICIENT_MARGIN` против `available`**~~ **Закрыто `765e678`** (раздел «29.09, день»), а не `equity` (хвост 26.09 ниже): маржа
  входа должна сравниваться со свободными средствами. Отдельным шагом, не в этих блоках

## 28.09 — деплой утра (`41a720f`, 15.6)

**Прод: `41a720f`, миграция `df411b3ca043`. Тестов 1078 (0 skipped), smoke 66/66.**
15.6 reconciler — на проде и проверен живьём.

### Деплой 28.09 ~04:57 UTC

`b634712..41a720f` поверх `d47f69c`: 15.6 целиком (разбор allFillOrders под живую форму,
фильтр импорта по `triggerOrderId`, история ордеров/позиций в клиенте,
`reconciliation_events` + `NOT_PLACED`, id биржи у сделок бота, решения и воркер
reconciler, монитор TP/SL — гистерезис/mark price/процент до 0.1, события в сводке,
раздел 10 ТЗ, лог цикла reconciler на DEBUG).

- Префлайт: md5 `app/` = `d47f69c` (116, с отрицательным контролем), PENDING/UNKNOWN 0,
  строк моложе 5 мин нет, `exec:lock` 0, ENTRY в UNKNOWN/PENDING/SUBMITTED — 0
- Точка отката до build: `trading_bot-bot:rollback_20260928_044423` → `bde832cb5714`
  (проверено `docker image inspect`). **Держим** до первого естественного закрытия
  LINK #3 или 24 ч, удаление — с «да» владельца
- Дамп/снапшот `pre_deploy_20260928_044436` (600, `gzip -t`, #4 и её исполнения внутри),
  md5 всех 209 файлов = `41a720f`, образ `e8c5db2c2e98`
- Stop → upgrade (одна строка `Running upgrade 407583974eb1 -> df411b3ca043`, таблица и
  три индекса) → правка #4 → up 04:57:41 UTC. 0 рестартов, 0 ERROR, md5 `app/` в
  контейнере = `41a720f` (121)

**SOL #4 до деплоя была закрыта вручную** 27.09 17:19:55 UTC по 121.000 без комиссии
выхода (PnL −2836.52 против −2086.88 у биржи). Решение владельца — вернуть в OPEN, чтобы
reconciler закрыл её фактом: одной транзакцией удалён ручной выход (исполнение #7) и
сброшены поля выхода, проверка «до/после» с ROLLBACK, затем COMMIT после миграции при
остановленном боте. Обратный SQL держали до проверки, потом удалён.

### Живая проверка reconciler (первый цикл, 04:58:50 UTC)

| | Ожидалось | Факт |
|---|---|---|
| #4 status | CLOSED | CLOSED |
| exit_price | 121.611 | 121.611 |
| fees | 166.602903 | 166.602903 (83.78152 + 82.821383) |
| pnl | ≈ −2087.12 | −2087.121603 = (121.611 − 123.021) × 1362.07 − 166.602903 |
| exit_reason | «Стоп-лосс на бирже» | «Стоп-лосс на бирже» |
| closed_at | 27.09 14:40:36 UTC | 27.09 14:40:36 UTC |
| external_fill_id выхода | …712320 | 2104219661398712320 |
| execution_orders #41 / #42 | FILLED / CANCELED | FILLED / CANCELED |
| reconciliation_events | одно по #4 | одно: CLOSED_STOP_LOSS, notified_at проставлен |
| LINK #3 | не тронута | OPEN, fees 14.672461, только вход; событий по LINK 0 |
| Биржа LINK (GET) | позиция = журнал | LONG 2037.8 = quantity #3; SL 13.526 / TP 16.263 живы |

### Смена бота 28.09 ~06:23 UTC

Бот — **`@postfactumTrade_bot`, id 8867032918** (отображаемое имя «PostfactumBot»).
Старый — `@elmir_trading_journal_bot`, id 8690367328: **бот удалён 28.09, токен
недействителен** (удалён владельцем в BotFather; getMe со старым токеном — HTTP 401,
проверено 28.09 ~12:20 UTC). Кода не меняли.

- Токен приходит в контейнер только через `env_file: .env` в compose: в образе его нет
  (`.dockerignore` — `.env`, `.env.*`), проверено по текущему образу и rollback-образу
  (`/app/.env` нет, `BOT_TOKEN` в ENV образа нет). Поэтому смена — правка
  `/opt/trading_bot/.env` + `up -d --no-deps --force-recreate bot`, **без пересборки**;
  откат на rollback-образ поднимет бота с тем токеном, что в `.env`
- Замена в `.env` — python-скриптом через stdin: проверка формата, ровно одна строка
  `BOT_TOKEN`, временный файл 600 + `os.replace`; после — изменился только ключ
  `BOT_TOKEN` против `.env.bak_20260928_061729`. Токен не печатался: только префикс id,
  длина, sha256[:8] (новый `ec1ef6d0`, старый `f2ab595b`)
- `.env.bak_20260928_061729`, `.env.bak.20260905213209`, `.env.bak_20260907_012224` —
  удалены 28.09 после getMe = 401 (все три отличались от `.env` только старым `BOT_TOKEN`)
- После recreate: getMe и лог — `@postfactumTrade_bot` id 8867032918, polling, 0 ERROR /
  Unauthorized / Conflict, 0 рестартов. Меню команд бот выставляет сам при старте
  (`setup_bot_ui`, `app/main.py`). `/start` владельца — сработал, меню на месте,
  «Открытые» показывает LINK #3. LINK #3 и события сверки не изменились
- Данные не терялись: всё привязано к `telegram_id` пользователя, не к боту

### Флаги на проде (28.09, до вечернего деплоя; с `42efa3f` TTL лока 183)

`exec_dry_run=False`, `bingx_trading_mode='demo'`, `exec_allow_live_mode_orders=False`,
`trading_execution_enabled=True`, `confirm_lock_ttl_seconds=173`,
`reconciler_interval_seconds=60`, `reconciler_stop_check_every=5`. Открыта одна сделка
бота — LINK #3 (демо).

### Новые хвосты 28.09

- ~~**PnL журнала против `netProfit` биржи**~~ — **закрыто в коде 28.09 вечером
  контролем** (`4d7d8bc`, на проде с `42efa3f`): `pnl` журнала — наш расчёт из цен, `profit`
  биржи у выхода, расхождение > 0.01R — `PNL_MISMATCH`. Исходный разбор:
  #4: журнал −2087.121603, биржа `netProfit` −2086.8778 (0.24). Комиссии совпадают
  (166.602903 против `positionCommission` 166.602902565), разница — в цене: биржевая
  разница цен `realisedProfit / qty` = −1920.2749 / 1362.07 = −1.40982, наша по `avgPrice`
  −1.41 — **гипотеза подтверждена арифметикой**: `avgPrice` округлён до 3 знаков, биржа
  считает по неокруглённым. Вопрос: `pnl` журнала — наш расчёт из цен (как сейчас) или
  `realisedProfit` биржи
- ~~**Уведомления reconciler о закрытии — сырые Decimal.**~~ **Закрыто `e22d19c` (вне бота) и `972df0b` (остальные).** Было «82.821383»,
  «−2087.121603» (`fmt_decimal`): все суммы — через `fmt_money`/`fmt_price`/`fmt_qty`,
  плюс строка «Закрыта: ДД.ММ ЧЧ:ММ» в часовом поясе пользователя. Тест на текст.
  При реализации `fmt_money`/`fmt_qty` (и `fmt_ratio`/`fmt_percent`/`fmt_amount`)
  перенести из `app/bot/formatting.py` в `app/core/numfmt.py`, `formatting.py` их
  реэкспортирует, воркер импортирует только из `app.core`. Сейчас в `numfmt.py` только
  `fmt_num` и `fmt_price`
- ~~**Пульс reconciler в сводке исполнения**~~ — **сделано в коде 28.09 поздним вечером**
  (`90fae6e`, не задеплоено): INFO раз в 60 запусков и строка «Сверка:» в сводке
- **Частичное закрытие не проверено воркером.** `decide_trade` частичный выход
  разбирает (юнит-тест `test_partial_close_keeps_trade_open`: позиция уменьшилась —
  выход на уменьшение, `PARTIAL_CLOSE`, сделка открыта), но теста воркера (запись
  исполнения, сделка остаётся OPEN, уведомление) нет, живьём не встречалось
- Хвосты 26.09 (раздел ниже) остаются открытыми, кроме закрытых этим деплоем: разбор
  allFillOrders, импорт rx/дочерних ордеров бота — частично (фильтр по
  `triggerOrderId`; rx-ордера разведки 27.09 всё ещё подтянутся), монитор без
  гистерезиса/округления, сводка слепа к реальным входам

## 26.09 — предыдущее состояние (хвосты и уроки ещё действуют)

**Прод: `d47f69c`, миграция `407583974eb1`. Тестов 1033, smoke 66/66.**

### Деплои

| Когда | Что | Миграции | Снапшот |
|---|---|---|---|
| 24.09 19:14 | 15.5.3 + 15.5.4 | да (голова `0662dd79bd76`) | `pre_deploy_20260924_191434` |
| 26.09 07:49 | 6 коммитов `5b3e658..9feb11b` | нет | `pre_deploy_20260926_074736` |
| 26.09 20:09 | `1e6cb4f` коммит сканера после каждого слота | нет | `pre_deploy_20260926_200754` |
| 27.09 05:14 | `.dockerignore` (`5f5d502`) + запрет сети в smoke_check (`d325cd2`) | нет | `pre_deploy_20260927_051247` |
| 27.09 ~18:00 | `b634712..d47f69c`: card_price/card_quantity, валюта баланса, «Вход исполнен» с четырьмя ценами, сводка по реальным входам, аномалия REJECTED, зона сигнала на карточке | да (`407583974eb1`, 2 колонки nullable) | `pre_deploy_20260927_175433` |

Деплой 26.09 07:49: md5 всех 194 файлов = `9feb11b` (до и после сборки), `alembic current` =
`heads` = `0662dd79bd76` (только чтение, upgrade не запускался), `trading_bot` 0
рестартов, в логе после старта ошибок нет.

Деплой 26.09 20:09: md5 всех 195 файлов = `1e6cb4f` (до и после сборки), `alembic current` =
`heads` = `0662dd79bd76` (только чтение), `trading_bot` запущен 20:09:23 UTC, 0 рестартов,
лог после старта без ошибок, override с `EXEC_DRY_RUN: "false"` пережил деплой.
Префлайт: PENDING/UNKNOWN 0, `exec:lock` 0, позиции на демо пусты.
Сканер на `1e6cb4f` (лог 26.09 20:09 – 27.09 04:39 UTC): 16 циклов «Цикл сканера завершён»
каждые 30 мин с 20:39:42, `symbols_scanned=10`, 6–11 с; ERROR/Traceback/«упал» нет,
только 4 WARNING `matplotlib.font_manager` (findfont, шрифт подменён), 0 рестартов.

Деплой 27.09 05:14: до build — md5 `app/` = `1e6cb4f` (116 файлов), PENDING/UNKNOWN 0,
строк моложе 5 мин нет, `exec:lock` 0. md5 всех 196 файлов = `5f5d502`. Новый образ
`b11e08849a42` проверен до переключения (`compose run`, бот не стартовал): в `/app` нет
`.env`, `.env.*`, override; имена ключей Settings из окружения совпали (32 = 32); md5
`model_dump` с развёрнутыми `SecretStr` совпал. Каждая проверка — с отрицательным
контролем. После up: 0 рестартов, лог без ошибок, флаги прежние (`exec_dry_run=False`,
`demo`, `allow_live=False`), `/app/.env` в контейнере нет. Первый цикл сканера 05:45:14,
`symbols_scanned=10`, без ошибок. Уборка: старый образ `9d3895ec7995` (с `.env`) удалён,
кэш сборки 2.08 ГБ очищен (`builder prune -af`), образ `trading_bot` на сервере один.

Деплой 27.09 ~18:00 (с миграцией): до build — md5 `app/` = `5f5d502` (116 файлов, с
отрицательным контролем), PENDING/UNKNOWN 0, строк моложе 5 мин нет, `exec:lock` 0. Дамп и
снапшот `pre_deploy_20260927_175433` (600, `gzip -t`, 42 строки `execution_orders`). md5
всех 197 файлов = `d47f69c`. Образ `bde832cb5714`; офлайн-SQL — два `ADD COLUMN ... NUMERIC(28,
12)` + `alembic_version`, «да». Stop 17:59 → upgrade (одна строка `Running upgrade`) →
схема: `card_price`, `card_quantity` numeric(28,12) nullable → up 18:01:50. После:
`current` = `heads` = `407583974eb1`, md5 `app/` в контейнере = `d47f69c`, флаги прежние
(`exec_dry_run=False`, `demo`, `allow_live=False`), 0 рестартов, лог без ошибок. **Образа
отката нет:** тег на старый `b11e08849a42` не встал — образ пропал после сборки нового; откат
только пересборкой из снапшота (downgrade не нужен — колонки nullable). Первый цикл
сканера на `d47f69c`: 18:32:08 UTC, `symbols_scanned=10`, 7.73 с, без ошибок, 0 рестартов.

### Что задеплоено

- **15.5.3** read-back и спасение стопа
- **15.5.4** сделка в журнал из факта (`trades.fill_confirmed`)
- **Отказ биржи не сжигает сетап** (вариант А): REJECTED не блокирует новое уведомление
  по тому же сетапу. Повтор по тому же уведомлению после REJECTED — нет, номер попытки
  в clientOrderID не вводим. Новое уведомление по тому же сетапу — разрешено
- **15.5.4а гвард живой позиции** `EXCHANGE_POSITION_EXISTS`: на карточке в `evaluate()`,
  на «Да» в `_submit_real_order` тем же клиентом до `adjust_leverage`. Сбой чтения
  позиций ≠ «позиций нет» → ERROR-строка
- **Трейс сбоя позиций** в логе (`162997e`)
- **Валидатор `EXEC_DRY_RUN` снят**: `EXEC_DRY_RUN=false` больше не роняет запуск
- **Коммит сканера после каждого слота** (`1e6cb4f`), а не один на проход
- **`.env` не копируется в образ** (`5f5d502`, `.dockerignore`): секреты в контейнер
  только через `env_file` в compose. Ключ, пропавший из `.env`, молча уйдёт в дефолт
  Settings — файла в контейнере нет

### Флаги на проде (Settings изнутри контейнера, 26.09)

| Параметр | Значение |
|---|---|
| `exec_dry_run` | `False` (с 26.09 19:36) |
| `bingx_trading_mode` | `'demo'` |
| `exec_allow_live_mode_orders` | `False` |
| `trading_execution_enabled` | `True` |
| `confirm_lock_ttl_seconds` | `173` |

Что это значит:
- **`EXEC_DRY_RUN=false` включён в override 26.09 19:36.** Копия override до включения —
  `/opt/backups/override_pre_155_20260926_193528.yml`. Откат: `cp` копии на место
  override + `docker compose up -d bot`
- **Следующее «Да» на READY — реальный ордер на VST-хост** (демо): риск 2% ≈ 1777 VST,
  плечо ×10 из плана
- **Бой закрыт** `EXEC_ALLOW_LIVE_MODE_ORDERS=false` и тремя слоями
  `LIVE_ORDERS_NOT_ALLOWED`: A — `evaluate()`, B — `run_guards()`, C —
  `_submit_real_order`. Слои A и C покрыты тестами пути, каждый падает при отключении
  своего слоя. Слой B проверяют только юнит-тесты `test_guards.py` — отключение B тесты
  пути не ловят
- **TTL лока 173 с** (было 163, +1 запрос позиций на «Да»)

### Живая форма `/user/positions` (демо, хедж, только GET)

| Прогон | Состояние | `data` | `positionSide` | `positionAmt` / `availableAmt` |
|---|---|---|---|---|
| a | позиций нет | `list`, 0 | — | — |
| b | LONG открыт | `list`, 1 | `LONG` (str) | `0.8397` / `0.8397` (str) |
| c | LONG закрыт | `list`, 0 | — | — |
| d | SHORT открыт | `list`, 1 | `SHORT` (str) | `1.2592` / `1.2592` (str), **положительное** |
| e | SHORT закрыт | `list`, 0 | — | — |

23 ключа на запись, в b и d одинаковые. Закрытая позиция пропадает из списка, а не
остаётся с нулём. Парсер строгий: не список, нет/неизвестный `positionSide`, пустое
или отрицательное количество → ошибка, запасного `availableAmt` нет.
- **`BOTH` = явная ошибка** `UnsupportedPositionMode`, форма ответа one-way не разведана
- **Переключение счёта в one-way блокирует входы**, пока эту форму не разведаем

### Очередь

1. ~~**15.5.5 шаг 2** — префлайт и включение `EXEC_DRY_RUN=false` в override~~ —
   **выполнено 26.09 19:36**, ждём первый READY
2. **Первый демо-ордер**
3. **Разведка полей исполнения** — только GET
4. **15.6 reconciler**
5. ~~**Смена бота**~~ — **выполнено 28.09**, `@postfactumTrade_bot` (раздел 28.09)
6. ~~**Цель 2R**~~ — **решено 28.09**, блок B `2fad4c7` (`TargetSource` LEVEL / FORMULA_2R,
   «Цель: уровень X» / «Цель: 2R по формуле — X» в уведомлении), на проде с `42efa3f`
7. **15.7**

### Новые хвосты

- **`leverage` в парсере позиций** — дефолт только для отображения: живой тип поля не
  разведан, на решения он не влияет
- ~~**`INSUFFICIENT_MARGIN` сравнивает маржу с equity, а не с available.**~~ **Закрыто `765e678`.** Исправить до
  15.7, после первого ордера — **решение 28.09: отдельным шагом сразу после блоков
  A/D/CROSSED/B/C**
- **Импорт истории подтянет rx-ордера разведки 27.09 в журнал.** Фильтр
  `_drop_bot_fills` (`app/services/import_service.py:219`) видит только `orderId` из
  `execution_orders`, а ордера разведки туда не писались: вход `2104117391101272064`
  (`rx1790495653136x84c65e48`) и выход `2104117401649946624` (`rx1790495655671xd85fb1f2`),
  BTC-USDT LONG 0.0001, демо, 27.09 07:54 UTC — склеятся в закрытую сделку. **На демо
  импорт не нажимать до решения**
- **Экран «Ордера на бирже» считает прикреплённые TP/SL бота ручными.**
  `_is_own_order` (`app/bot/handlers/exchange.py:202`) смотрит префикс `tj`, а у
  вложенных TP/SL `clientOrderId` пустой. «Свои» определять по `orderId` из
  `execution_orders.exchange_order_id`. На решения не влияет — только отображение
- **`abs(commission)` в трёх парсерах превращает ребейт в расход** (`bingx.py`
  `_parse_fill`, `_parse_order`, `_parse_order_fill`). Живьём расход — отрицательная
  `commission`; ребейт `+x` уйдёт в журнал как fee `x`, PnL занижен на `2x`. Чинить по
  живому мейкер-исполнению
- **«USDT» в карточке исполнения на демо** (`app/bot/handlers/execution.py:260`) —
  валюта захардкожена, на демо VST
- **Лог пути «Да» не пишет плечо до/после `set_leverage` и был ли POST, и число
  попыток `get_order` в read-back.** Без этого разбор первого ордера (#3, 27.09)
  держится на косвенных выводах (плечо «было 20» — по short=20 на бирже)
- **Остаток лимита BingX на торговых ручках мал:** на `/trade/leverage` было 4, на
  `/trade/order` 9, троттлинг добавил к пути «Да» ~2 с. **Окно — 1 с, не 10**
  (`X-RateLimit-Requests-Expire` ≤ 1000 мс, снято 27.09). Пределы за 1 с (GET, демо):
  `/trade/order` 30, `/user/positions` 10, `openOrders`/`allOrders`/`allFillOrders` 5,
  `positionHistory` 40; POST `/trade/order` и `/trade/leverage` — остатки 9 и 4, то есть
  ~10 и ~5. Снять пределы POST на боевом хосте до 15.7
- **Цена показа карточки нигде не сохраняется, «карточка» в «Вход исполнен» смешивает
  дрейф и проскальзывание.** `planned_price` живёт только в памяти (`_confirmations`);
  `execution_orders.price` — перезапрос на «Да». Сделка #3: карточка 14.393 → на «Да»
  14.398 → исполнение 14.4; сообщение показало 0.05% (14.26) — это дрейф 10.19 +
  проскальзывание 4.08. Объём тоже пересчитывается на «Да»: по расчёту на карточке
  ~2049.6, исполнено 2037.8 (сверить со скриншотом карточки)
- ~~**До 15.7, обязательно: логировать callback исполнения**~~ **Закрыто в коде `83c3bf0`**
  (`execution_callbacks`, раздел «01.10, этап 15»), задеплоено 01.10 14:22 UTC. Было: (`exn:open` / `exn:yes` /
  `exn:no`) — `notification_id`, маскированный `telegram_id`, `message_id`, время. Сейчас
  нажатия не пишутся никуда: «Да» по SOL 27.09 (#4) подтверждено только выводом из кода
  (состояние карточки + whitelist из одного id). На реальном счёте «кто нажал» должно
  подтверждаться записью
- **15.6: сверка закрытия с `positionHistory` не подключена.** Ручка разобрана
  (`BingXClient.get_position_history`: avgClosePrice, closePositionAmt, netProfit,
  positionCommission), но reconciler решает только по `allOrders` — вторая проверка
  итога закрытия не делается
- **15.6: отчёт по демо-периоду по кнопке не сделан** (раздел 12а ТЗ: входы, закрыто
  по SL / TP / вручную, фактический средний R, расхождения)
- ~~**Ротация `logs/bot.log`**~~ — **не хвост** (чек-ап 28.09): файл ротируется
  `RotatingFileHandler` (10 МБ × 5, `app/core/logging.py`), на проде 2.3 МБ, ~120 КБ/сутки
- **Сводка исполнения слепа к реальным входам.** Дрейф, риск % и RR копятся только по
  `DRY_RUN` (`app/workers/execution_digest.py:202-210`); `FILLED`/`SUBMITTED` — только
  счётчики. С `EXEC_DRY_RUN=false` отклонение риска и аномалия `PRICE_DRIFT` для
  настоящих входов не срабатывают

### Закрытые хвосты

- **Коммит сканера после каждого слота** (`1e6cb4f`): кнопка до конца прохода больше не
  даёт «устарело», «Да» не ждёт блокировку строки слота весь проход
- **`.env` в Docker-образе** (`5f5d502`, задеплоено 27.09): `.dockerignore`, старый образ
  и кэш сборки со слоями `.env` удалены. `.env` по-прежнему лежит в снапшотах
  `/opt/backups/*.tar.gz` (600, так задумано) и в двух `.env.bak*` на хосте (600,
  05.09 и 06.09) — решение по ним за владельцем
- **smoke_check без сети** (`d325cd2`): транспорты httpx sync/async и
  `aiohttp.ClientSession._request` запрещены на классе; любой запрос — `✗ СЕТЬ` и exit 1.
  Проверено снятием заглушки `get_positions` и прямым `getMe`

### Разведка ордера на демо 27.09

Инлайн в прод-контейнере, вне пути бота (карточка/гварды/БД/журнал не участвовали),
с явного «да» владельца. BTC-USDT LONG 0.0001 маркет, TP/SL ±2% вложенными, затем
маркет-закрытие. Финал: позиций и openOrders по BTC-USDT 0, комиссия 0.004231 VST на
вход и на выход.

- **Подпись POST с `takeProfit`/`stopLoss` проходит** — код `_build_tp_sl` +
  `_build_signed_query` как есть, ответ `FILLED`
- **clientOrderID длиной 24 принят** (`rx…`, ASCII буквы/цифры), вернулся эхом
- **Поля исполнения, GET `/trade/order`:** `status`, `avgPrice`, `executedQty`,
  `origQty`, `commission` — все **str**; `commission` **отрицательная** (`-0.004231`);
  `orderId` — **int** (19 цифр); `time`/`updateTime` — int, мс. Ключ clientOrderID в
  GET только `clientOrderId`; в ответе POST оба написания (и `orderID`/`orderId`).
  `get_order_fill` разобрал без `ReadbackIncomplete`
- **Позиция:** `positionSide` str, `positionAmt` str
- **Вложенные TP/SL в openOrders:** отдельные ордера SELL/LONG, `workingType=MARK_PRICE`,
  stopPrice = отправленному, **`closePosition=False`, `clientOrderId` пустой**.
  `find_our_conditional` ни того, ни другого не требует
- **TP/SL снимаются биржей сами при закрытии позиции** — отмена не понадобилась
- Не проверено: `set_leverage` POST (на демо BTC плечо 20, в плане 10 — «Да» его
  вызовет), спасение стопа `place_conditional_order` c `closePosition=true`

### Разведка BTC (26.09, только чтение)

- **READY по BTC редки из-за рынка** (боковик, узкий ATR), а не из-за правил кода. В БД
  по BTC 1 READY (H1, 08.09) и 7 FORMING. Прогон детекторов по 500 свечам H1/H4:
  `validate_geometry` не срезала ни одной точки ни по одному символу, RR < 2 режет BTC
  не чаще альтов, точность цены и дистанция стопа BTC не выделяют
- **Стратегия для боковика — «третья стратегия»**, решение по статистике

### Уроки 26.09

- **Тест, названный «слой C», проверял слой A.** Какой слой проверяет тест —
  выяснять отключением слоя, а не по docstring
- **К проду только последовательные ssh-вызовы.** Два параллельных — sshd рвёт одно
  соединение («Connection closed»)
- **Точка отката — тег образа ДО build, с проверкой `docker image inspect`.** 27.09 старый
  образ `b11e08849a42` после сборки нового остался безымянным и пропал до stop; тег по
  id уже не встал — откат только пересборкой из снапшота. Процедура в `CLAUDE.md`
  (`049618e`): `rollback_<ts>` до build, удаление — после чистого цикла сканера и «да»
- **Скрипты на сервер — только через stdin, не строкой в ssh.** 28.09 экранирование
  внутри `ssh '…'` дважды дало ложные числа по `.env` (`tr -d "…\x27"` и
  `tr -d \\042…` удалили цифры токена из подсчёта: «префикс 86903638», длина 41).
  Правильный ответ дал python-скрипт, переданный `ssh … python3 - < file` / heredoc.
  Ноль экранирования — ноль ложных чисел
- **Код выхода конвейера — код последней команды.** `pytest | tail -1 && git commit`
  закоммитил упавший прогон (27.09, `9ae8e29`): `tail` вернул 0. Только
  `set -o pipefail` или проверка по выводу/файлу, не по `&&` после пайпа
- **Ноль от несработавшей проверки неотличим от чистого результата.** `tar tf` с путём
  `C:` показал 0 секретов, не выполнившись. Проверку сначала заставить найти заведомо
  существующее

## Архив 24.09

**Прод без изменений: `107d0b0`, миграция `b0943282b974`, сухой прогон.**

В коде (запушено, НЕ задеплоено):
- **15.5.3** read-back и спасение стопа: `app/execution/readback.py::verify_entry`
  (идемпотентна, состояние в строках БД), строгий `get_order_fill` (нет поля →
  `ReadbackIncomplete`, не 0), `find_our_conditional` (8 признаков, `ambiguous` без
  спасения), спасение `STOP_MARKET`/`TAKE_PROFIT_MARKET` с `closePosition=true`, одна
  попытка. Уровни округляются к цене входа до сборки ордера (был дефект: уходили сырыми).
  Отдельное сообщение-тревога «ПОЗИЦИЯ БЕЗ СТОПА». **TTL лока 163 с.** Тестов 924
- **15.5.4** сделка в журнал из факта: `app/execution/journal_entry.py`. Сделка
  создаётся при любом исходе, кроме явного отказа (журнал кормит гварды —
  POSITION_EXISTS/MAX_TOTAL_RISK). Неподтверждённая — `trades.fill_confirmed=false`,
  цена плановая. Частичный UNIQUE `uq_trades_notification_id`. Импорт истории
  пропускает исполнения ордеров бота. Тревога «вход за стопом». Новая миграция после
  `b0943282b974`, репетиция на копии прода пройдена с двумя откатами

Проверки владельцем: старая кнопка → «устарело» ✅. Новый READY → карточка = уведомление — ждёт сигнала.

### Очередь с 24.09

1. **Деплой 15.5.3 + 15.5.4** (миграция) — промпт в чате готов. **Не начинать при
   малом остатке лимита Claude Code**: деплой с миграцией, оборванный после
   `stop bot`, оставит бота остановленным
2. **15.5.4а — гвард «живая позиция на бирже».** `POSITION_EXISTS` смотрит только
   журнал. Ручная позиция владельца на BingX по тому же символу/стороне в хедже
   сольётся с входом бота, а спасённый стоп `closePosition=true` закроет её целиком.
   Перед входом читать позиции с биржи, отказ при любой живой позиции по символу.
   +1 запрос в пути «Да» → пересчитать TTL. Выкатить вместе с 15.5.5
3. **15.5.5** первый демо-ордер (снимает валидатор, только с «да»)
4. **15.6** reconciler — сразу после 15.5.5: до него закрывшиеся на бирже сделки
   бота владелец закрывает в журнале руками, иначе символ заблокирован

## Архив 23.09

**Прод на `107d0b0`, голова миграций `b0943282b974`.** Всё задеплоено, откат не
понадобился. Сухой прогон: `EXEC_DRY_RUN=true`, `EXEC_DRY_RUN=false` роняет запуск
валидатором «до шага 15.5.5 — нет read-back и журнала».

| метрика | значение |
|---|---|
| pytest | **867 passed**, 0 skipped |
| smoke_check.py | 66/66 дважды, exit 0 (с `1703d71` exit ≠ 0 при провале) |
| ruff check . | `F821`=0, `RUF100`=4, `I001`=10, `E501`=55; остальное — кириллица `RUF001-003` |
| mypy app | 69 в 29 файлах — бэйзлайн |

Цепочка миграций: `ddace081d9fb` → `f3a92c7e1b0d` → `b4c1e9a7d203` → `4f3837b79361`
→ `c8e2d51a9f04` → **`b0943282b974`** (signal_notifications).

### Деплои 23.09

- **15.5.2** (`448b0e4` + docs) — путь реальной отправки за флагом, no-op по миграциям
- **15.5.2а** (`107d0b0`) — миграция `b0943282b974`, репетиция на копии прода пройдена
  с откатом. Снапшот `/opt/backups/trading_bot_pre_deploy_20260923_090610.*`

### Сервер — гигиена секретов (23.09)

- `.env`, override, все файлы `/opt/backups` — 600, каталоги — 700; `umask 077` в
  `backup_db.sh` (проверено ручным запуском строки из crontab — дамп 600 сам) и в
  шаге 1 деплоя (`CLAUDE.md`)
- zabbix-агент: `DenyKey=system.run[*]` → удалённых команд нет, **ключ BingX не
  перевыпускаем** (у него ещё и IP whitelist на сервер)
- **Токен бота** — сменён при переезде на нового бота 28.09; старый бот удалён 28.09,
  токен недействителен
- ufw выключен — сознательно: Docker публикует порты мимо ufw, пользы мало. Защита —
  п.4 чек-апа (5432/6379 не слушают на хосте)

### 15.5.2 — отправка ордера (в коде, за флагом)

- `PENDING` → **`session.commit()` до HTTP** и **commit после ответа** (раньше коммит
  был один на хендлер — раздел 8 был защитой на бумаге)
- `REJECTED` только при `exc.code` не `None` и ≠ 0. Сбой разбора, транспорт, любое
  не-`ExchangeError` после POST — `UNKNOWN` (широкий `except` здесь осознанно, с
  `logger.exception`)
- одна HTTP-попытка — тест уровня клиента с `MockTransport`
- клиент отправки строится от той же настройки, что проверяет `LIVE_ORDERS_NOT_ALLOWED`
  (`bingx_allowed_exchange_mode` вычисляется из `bingx_trading_mode`) + перепроверка до HTTP
- плечо: GET → `leverage_needs_update` по стороне → POST только при расхождении,
  `position_side` всегда `LONG`/`SHORT` в хедже; сбой → `LEVERAGE_FAILED`
- нет `orderId` при `code 0` → `SUBMITTED`, `exchange_order_id=None`, честный текст
- тексты: `UNKNOWN` — «⚠️ Биржа не ответила. Ордер мог пройти — проверь позиции
  в BingX. Повторно не отправляю.»
- **`UNKNOWN` не переотправляется автоматически никогда** (и после 15.6). `PENDING`
  старше окна reconciler разбирает как `UNKNOWN`

### 15.5.2а — уведомление как неизменяемая сущность

Корень бага: «сигнал» был изменчивой строкой слота `(user, symbol, tf, level)`,
сканер перезаписывал уровни и даже направление. «Да» исполнял **не то, что на
карточке**; слоты сгорали навсегда; второй вход давал тот же `clientOrderID`.

- таблица `signal_notifications` — снимок на каждое **отправленное** уведомление:
  уровни, направление, fingerprint, `notified_at`, собственный `expires_at` = +4 ч
  (не сдвигается пересканами), `trade_opened_at`
- снимок и отправка в одном SAVEPOINT: недоставлено → снимка нет → повтор следующим сканом
- READY переуведомляется по **последнему снимку** (fingerprint изменился или снимок
  истёк); FORMING — по старому правилу слота, кнопок не несёт
- кнопки `exn:*:{notification_id}`; старые `exec:*:{signal_id}` → «⏳ Уведомление
  устарело»
- карточка и «Да» — **из снимка**; на «Да» `SELECT … FOR UPDATE` слота + `refresh`
  снимка под блокировкой
- новые отказы: `SIGNAL_SUPERSEDED` (fingerprint слота ≠ снимка), `SIGNAL_EXPIRED` по
  снимку, **`SETUP_ALREADY_TRADED`** — повторный вход в тот же сетап (слот +
  fingerprint) запрещён, решение владельца
- `clientOrderID` = `tj{notification_id}u{user_id}{E|S|T}`, ≤ 24 символа;
  `setval` последовательности выше `MAX(signals.id)` — не пересекается со старым форматом
- `execution_orders.notification_id`, `trades.notification_id` — nullable FK;
  `signals.trade_opened_at` код больше не читает (удалить отдельной миграцией позже)
- сводка: READY считается по снимкам (события), добавлены `SUBMITTED/REJECTED/UNKNOWN/PENDING`

### Ждут владельца — проверка руками

1. Старая кнопка «⚡ Открыть сделку» (до деплоя) → «⏳ Уведомление устарело»
2. Первое READY нового образца → «⚡ Открыть сделку» → стоп и тейк на карточке =
   тексту уведомления. «Да» на сухом прогоне ордера не создаёт

### Очередь

1. Проверки руками (выше)
2. **15.5.3** read-back и спасение стопа — план по файлам. Парсер терпимый: неизвестное
   поле → ошибка и уведомление, не ноль. `_CONFIRM_PATH_HTTP_CALLS` обновить в том же шаге
3. **15.5.4** сделка в журнал из факта (`trades.notification_id` уже есть)
4. **15.5.5** первый демо-ордер = разведка полей исполнения, подписи с TP/SL, лимита
   `clientOrderID`. Снимает валидатор. Только с «да»
5. 15.6 reconciler → смена бота (старый бот удалён 28.09, токен недействителен) → решение по цели 2R → 15.7

### Хвосты после 23.09

- **Сканер коммитит раз в конце прохода**, а сообщение с кнопкой уходит раньше:
  нажатие в эти секунды → ложное «устарело», блокировка слота на «Да» ждёт весь проход.
  Сейчас проход ~7 с. **Обязательно до расширения списка символов**: коммит после
  каждого доставленного уведомления
- ~~**`.env` запекается в Docker-образ**~~ — **закрыто, неверно для текущих образов**:
  `.dockerignore` (с `5f5d502`) исключает `.env` и `.env.*`, токен и прочие секреты —
  только через `env_file` в compose. Проверено 28.09 по текущему образу и
  rollback-образу: `/app/.env` нет, `BOT_TOKEN` в ENV образа нет
- `check_redis` пишет в Redis — нужен режим только чтения для чек-апа
- дата в `TextFormatter` (в `bot.log` только время)
- `ENVIRONMENT=prod` через override (косметика)
- миграция удаления `signals.trade_opened_at`
- `docker builder prune` (~2 ГБ), диск 28 ГБ, свободно 22
- в спеке/коде раньше были комментарии со старым форматом `clientOrderID` — исправлены в 15.5.2а

### Уроки 23.09

- **Исполнять ровно то, что показано.** Карточка и «Да» обязаны читать один
  неизменяемый объект; изменчивое «текущее состояние» для подтверждения не годится
- **Идентичность события ≠ идентичность слота.** Всё, что ключуется по id
  (флаг «использован», идемпотентность, лок, аналитика), наследует путаницу
- **exit code харнесса проверять**: `smoke_check` годами выходил с 0 при провале
- **chmod руками не доказывает umask** — проверка только реальным запуском
- **Миграцию с бэкфиллом — репетировать на копии прода** с откатом, а не на синтетике
- **Широкий `except` честен ровно там, где исход действительно неизвестен** — после
  ушедшего запроса; с полным трейсом


---

# Архив: handoff 22.09 (история, не текущее состояние)

## Как мы работаем

- **Новый чат первым делом подключает папку `C:\Users\vladz\OneDrive\Desktop\trading`**
  (запрос доступа к папке) и читает `trading_bot/docs/handoff.md` и
  `trading_bot/docs/architecture.md` (до 28.09 — корень `trading/`). Через неё
  Opus читает код напрямую и сверяет отчёты Claude Code с файлами
- Архитектура и решения — чат с Opus. Код и деплой — Claude Code в терминале
  владельца: `cd C:\Users\vladz\OneDrive\Desktop\trading` → `claude`
- Opus пишет промпт одним блоком (копируется одной кнопкой), владелец вставляет
  в Claude Code, возвращает отчёт
- План по файлам до кода на всём, что трогает больше двух модулей
- `/clear` между шагами. Разведку и реализацию не смешивать
- Новый тест обязан падать на старом коде — проверяется явно через `git stash`
  исходников. Тест-замок (проходит на обеих ревизиях) так и называется, за упавший
  не выдаётся
- Прод-команды Claude Code гейтит классификатор авто-режима. Подтверждать каждую
  команду, разрешение на сервер целиком не открывать

## Состояние на вечер 22.09

**Прод на `c472018`.** Голова миграций **`c8e2d51a9f04`**, за день ни одной миграции.
Все три контейнера up, логи без ошибок.

| метрика | значение |
|---|---|
| pytest | **787 passed**, 0 skipped |
| smoke_check.py | 66/66, два прогона подряд чисто |
| mypy app | 69 ошибок в 29 файлах — бэйзлайн, не растёт |
| ruff check . (весь репо) | 2407, почти всё `RUF001-003` (кириллица); `F821`=0, `RUF100`=4, `I001`=10 |

Цифры ruff в прошлой версии этого файла («`RUF100`, `I001` — 0, ~12 `E501`») сняты
по другому периметру — какому, не выяснено. Сравнивать только одной командой.

История числа тестов: 728 (`42bf9aa`) → 748 (`979c8f1`) → 782 (`2d9a4d8`) → 787 (`c472018`).

**Исполнение — по-прежнему сухой прогон**, но теперь флагом, а не зашитым кодом:
`EXEC_DRY_RUN=true` по умолчанию, `EXEC_DRY_RUN=false` роняет запуск валидатором
`Settings` (проверено живым запуском). Ветки реальной отправки в коде нет —
появится в 15.5.2. `place_market_order`/`set_leverage` нигде не вызываются.

### Деплои 22.09

| время (UTC) | коммит | что | снапшоты `/opt/backups/` |
|---|---|---|---|
| 14:09 | `29a2c26`, `979c8f1` поверх `d7781d2` | фикс подписи BingX, фикс гонки в тесте | `…_20260922_140957` |
| 18:29 | `2d9a4d8` | шаг 15.5.1 | `…_20260922_182937` |
| 18:48 | `c472018` | шаг 15.5.1а, логирование | `…_20260922_184813` |

Прошлая версия handoff считала прод на `42bf9aa` — отставала на два коммита.
Перед деплоем фактическое состояние прода сверяется по md5, а не по документу.

## Что сделано 22.09

### Подпись BingX (`29a2c26`)

Живая проверка — только `get_balance()` на DEMO, `code=0`, 89 002 VST. **Подпись
с TP/SL в запросе живьём не проверена** — это сделает первый демо-ордер (15.5.5).

### Разведка раздела 16 — только GET, снято с демо-хоста

- **`/openApi/swap/v2/quote/contracts` не отдаёт `maxLongLeverage`** → `SymbolInfo.max_leverage`
  всегда был дефолтом 20. Поле нигде в торговом пути не читалось (плечо берётся из
  `TradingPlan.max_leverage`) — удалено
- **`GET /openApi/swap/v2/trade/leverage?symbol=`** → `longLeverage`, `shortLeverage`,
  `maxLongLeverage` (150), `maxShortLeverage`, `availableLong/ShortVal/Vol`,
  `maxPositionLong/ShortVal`. Текущее плечо BTC — 20/20
- **Режим позиций: `GET /openApi/swap/v1/positionSide/dual`** (v1! v2 отвечает
  `code 100404`), значение в `data.dualSidePosition`. **Аккаунт в хедж-режиме**
  (`true`) → `positionSide` = `LONG`/`SHORT`, не `BOTH`
- `GET /openApi/swap/v2/trade/allOrders` — окно запроса **≤ 7 дней** (`code 109400`).
  Для reconciler в 15.6 — чанкинг по окнам
- `/openApi/v1/account/apiRestrictions` — **нет ключей `code`/`msg` вообще**, поля
  на верхнем уровне. `enableFutures=true`, `permitsUniversalTransfer=false`
- Позиций на демо нет. Исполненных ордеров за 7 дней нет → **имена полей факта
  исполнения (`avgPrice`, `commission`, `executedQty`) живьём не сняты**
- Лимит `clientOrderID` без создания ордера не проверить

### Шаг 15.5.1 (`caabaa5` … `2d9a4d8`, шесть коммитов)

1. `caabaa5` — убран `SymbolInfo.max_leverage`, рефакторинг без смены поведения
2. `8d1ec42` — `get_leverage()`, `LeverageInfo`, чистая `leverage_needs_update()`
   в `app/execution/leverage.py`: сравнение **по нужной стороне**
3. `18dd3a8` — `get_position_mode()` (v1-путь отдельной константой), модуль
   `app/services/position_mode.py`, приватный in-memory `TTLCache`
   (`EXEC_POSITION_MODE_TTL_SECONDS=300`), неудача не кэшируется. Гвард
   `POSITION_MODE_UNKNOWN` по образцу `PERMISSIONS_UNKNOWN`. Режим читается **только
   на построении карточки**, на «Да» несётся в `ExecutionQuote.dual_side_position`
4. `88ca785` — гейты: `EXEC_DRY_RUN` (дефолт true, false роняет старт до 15.5.2),
   `EXEC_ALLOW_LIVE_MODE_ORDERS` (дефолт false) и гвард `LIVE_ORDERS_NOT_ALLOWED` —
   второй после `EXECUTION_DISABLED`, до похода за правами ключа
5. `267d19d` — TTL лока 80 с: `ceil(10 × (6 + 1)) + 10`, где 6 — фактический
   худший confirm-путь (`get_ticker`, `get_balance`, `get_symbol_info`, `get_leverage`,
   `set_leverage`, `place_market_order`). Тест-замок `POSITION_EXISTS`
6. `2d9a4d8` — фейки `smoke_check.py` докручены до текущих сигнатур (три поломки
   каскадом маскировали друг друга)

Без миграции. Прод-поведение по умолчанию не изменилось, кроме одного: карточка
теперь ходит за режимом позиций и может отказать `POSITION_MODE_UNKNOWN`.

### Шаг 15.5.1а — логирование (`c472018`)

Текстовый форматтер (`LOG_JSON=false` на проде) **молча выбрасывал `extra={...}`
у всех строк**. Новый `TextFormatter` дописывает поля как `key=value`. Общая
константа `_RESERVED_LOG_RECORD_KEYS` для фильтра и обоих форматтеров — иначе
`message`/`asctime` задваивались бы как псевдо-extra.

Проверено: `SecretRedactingFilter` висит **на хендлерах** (stdout и file), обход через
дочерние логгеры исключён; `propagate=False` в проекте нигде нет. `httpx`/`httpcore`
на WARNING — URL с подписью не создаёт `LogRecord`. Скан логов прода (docker logs +
`/opt/trading_bot/logs/bot.log`) по `signature=`, `X-BX-APIKEY`, `apiKey`, `secret` —
**0 совпадений**.

## Решения, принятые 22.09

### Этап 15.5 разбит

- **15.5.1** гейты, плечо, режим позиций, TTL лока — ✅ в проде
- **15.5.1а** логирование — ✅ в проде
- **15.5.2** сборка `OrderRequest`, запись `PENDING` до HTTP, отправка за флагом,
  условный POST плеча, `bingx_position_side()`. Снимает старт-валидатор `EXEC_DRY_RUN`
- **15.5.3** read-back: чтение ордера по `clientOrderID` (до 3 попыток × 500 мс) и
  `openOrders`; признак прикреплённого стопа — `stopPrice != 0`. Стопа нет → отдельный
  `STOP_MARKET`, не вышло → `⚠️ ПОЗИЦИЯ БЕЗ СТОПА`. Парсер терпимый: неизвестное
  поле → явная ошибка и уведомление, не молчаливый ноль. `_CONFIRM_PATH_HTTP_CALLS`
  обновляется в этом же шаге
- **15.5.4** запись в журнал из факта (`avgPrice`, `executedQty`, `commission`)
- **15.5.5** первый демо-ордер — **он же разведка**: минимальный лот BTC, после
  входа печать ключей ордера, позиции, `openOrders`. Закрывает три пункта раздела 16
  разом: поля исполнения, подпись с TP/SL, лимит `clientOrderID`. С отдельным «да»

**Test-order не делаем** — он не покажет `avgPrice`/`commission`.

**`UNKNOWN` на 15.5 не переотправляется никогда** — только поиск по `clientOrderID`
и уведомление. Автоповтор из ТЗ п.8 — не раньше reconciler (15.6).

### `POSITION_EXISTS` — сравнение по символу, намеренно

В one-way встречный ордер нетто-закрывает позицию — путь «открыть сделку» молча
закрыл бы открытую. В хедже встречный вход — хеджирование, которого в ТЗ нет.
`MAX_TOTAL_RISK` по встречным стопам бессмыслен. Закреплено тестом-замком
`test_existing_position_refuses_opposite_side_too` и docstring гварда.

### 15.7 — обычный риск, узкая зона поражения

Владелец: **результат сделок — его риск**, работаем с обычным риск-процентом.
Отделено от **риска дефекта** (объём не по той цене, стоп не прикрепился, двойной
ордер) — от него гварды не убираются.

Вместо «риск 0.25%»:
- `EXEC_MAX_OPEN_POSITIONS=1`, whitelist из одного символа
- расширение — по счётчику: **5 исполнений подряд**, где факт с биржи сошёлся
  с журналом, SL/TP прикрепились с первого раза, ручного вмешательства не было

Preflight до первого боевого ордера (живыми запросами): `positionSide/dual` на
**боевом** хосте, права ключа и IP whitelist, баланс боевого счёта (сейчас 0 USDT —
sizing законно откажет), пробный расчёт объёма. Затем в override явно:
`BINGX_TRADING_MODE=live`, `EXEC_ALLOW_LIVE_MODE_ORDERS=true`, `EXEC_DRY_RUN=false`.

Что демо не докажет и снимается с первых боевых входов отдельной строкой сводки:
проскальзывание (карточка против `avgPrice`), комиссия (факт против демо),
частичное исполнение (`executedQty` против запрошенного), SL/TP (`stopPrice != 0`
чтением с биржи). Reconciler в 15.6 пишет фактическую комиссию — база сравнения
демо/бой копится сама.

## Ночь 22→23.09 — после деплоя 15.5.1а

### Закрыто (коммиты без деплоя, прод по-прежнему `c472018`)

- `environment=dev` на проде — **косметика**: читается только в строке лога старта
- `smoke_check.py` **всегда выходил с кодом 0**, даже при непройденных проверках
  («65/66, exit 0»). Починено `1703d71`. Прошлые «66/66» верны — их читали по выводу
- Эталонная команда ruff вписана в `CLAUDE.md` (`990f292`): `ruff check .` из корня
  `trading_bot`, ориентир `F821=0`, `RUF100=4`, `I001=10`
- Спека сведена с решениями 22.09 (`e2d9c53`)

### Решения

- **`UNKNOWN` не переотправляется автоматически НИКОГДА**, и после 15.6 тоже. Повтор =
  вход без подтверждения человеком по невиданной цене (нарушает раздел 0); «не
  нашёлся» ≠ «не выставлен». Окончательно «ордера нет» ставит только reconciler после
  окна. Сигнал остаётся использованным. `PENDING` старше окна reconciler разбирает как
  `UNKNOWN`
- **Валидатор `EXEC_DRY_RUN=false` живёт до 15.5.4 включительно**, снимается в 15.5.5.
  Без read-back и журнала реальный ордер не уходит
- `EXEC_TPSL_MODE` не вводим: TP/SL вместе со входом, запасной путь — отдельный
  `STOP_MARKET` в 15.5.3

### 15.5.2 — план принят с правками, отдан Claude Code на реализацию

Из плана Claude Code: `ExchangeError` получает `code`/`payload`; второй клиент через
фабрику для фазы отправки; `adjust_leverage` ловит сбой и `get_leverage`, и `set_leverage`
→ `LEVERAGE_FAILED`; **настоящий `session.commit()` строки `PENDING` до HTTP** (раньше
коммит был один на весь хендлер — раздел 8 был защитой на бумаге); одна ENTRY-строка
без SL/TP до 15.5.3; без миграции.

Правки Opus:
1. `REJECTED` — только при `exc.code` не `None` и не 0. Сбой разбора при `code 0`,
   `code=None`, транспорт — `UNKNOWN` (ордер мог пройти)
2. Ровно одна HTTP-попытка — тестом уровня `BingXClient` с замоканным транспортом,
   не фейком сервиса
3. Клиент отправки строится от той же настройки, что проверяет `LIVE_ORDERS_NOT_ALLOWED`
   (`bingx_trading_mode`), плюс отказ без HTTP при несовпадении режима клиента
4. Коммит и **после** ответа биржи — иначе сбой отрисовки откатит `SUBMITTED` в `PENDING`
5. **`clientOrderID` был неоднозначен**: `tj{signal}{user}` — (12,3) и (1,23) одинаковы,
   а `UNIQUE` глобальный. Новый формат `tj{signal}u{user}{E|S|T}`, ≤ 24 символов
6. `set_leverage` в хедже — только `LONG`/`SHORT`, не `None` и не `BOTH`
7. Текст `UNKNOWN`: «⚠️ Биржа не ответила. Ордер мог пройти — проверь позиции в BingX.
   Повторно не отправляю.»

Пять коммитов: исключения + тест одной попытки → `clientOrderID` → плечо → отправка
в хендлере → docs.

## Очередь

1. **Отчёт Claude Code по реализации 15.5.2** — сверить с правками выше
2. **Чек-ап прода** — промпт отдан, отчёта ещё нет. Нужен до деплоя 15.5.2. Проверяет
   сервер, часы (NTP — подпись зависит от timestamp), код против `c472018`, конфиг,
   логи за 24 ч, `execution_orders` на `PENDING`/`UNKNOWN`, залипшие `exec:lock:*`,
   BingX только GET, локальные тесты. В конце оформляется разделом в `CLAUDE.md`
3. Деплой 15.5.2 — no-op по миграциям, поведение прода не меняется (сухой прогон)
4. 15.5.3 → 15.5.4 → 15.5.5 (первый демо-ордер с «да»)
5. Смена бота — после 15.5, до 15.7
6. ~~**Цель 2R по формуле** — решить до 15.7~~ — решено 28.09, блок B `2fad4c7`

### Хвосты, не срочно

- `bot.log` без ротации — 1.69 МБ с 06.09, не горит
- уведомление «приближается к TP» печатает сырые проценты, двусмысленная подпись
- мёртвый блок `if …: pass` в `setups.py:339-345`
- `_describe(exc)` в `run_signal`/`run_scan` — старый широкий паттерн
- `import_service.py:155`, `market/data.py:105` — намеренные широкие `except` без пояснения
- права на **старых** снапшотах в `/opt/backups/` — в них `.env`, `chmod 600` только с 21.09
- `EXEC_SYMBOL_WHITELIST` в override — 10 символов, перед 15.7 сузить до одного
- хардкод «USDT» в текстах, `UserSettings.quote_currency` никто не читает
- расширение выбора символов (кнопочный выбор, FORMING по плану, READY по всем)
- `smoke_check.py` — 89 ошибок mypy, вне чистого периметра

## Новые уроки 22.09

- **Документ о состоянии прода устаревает быстрее, чем кажется.** Handoff отстал
  на два коммита за один день. Перед деплоем — md5 на сервере, не документ
- **Молчаливо выброшенное поле лога — та же ошибка, что молчаливый фолбэк.**
  `extra` терялся у всех строк с начала проекта, заметили, когда понадобилось число
- **Включая показ скрытого, проверь, что показываешь.** Рендер `extra` мог вынести
  секреты — проверили место фильтра (хендлер, не логгер) до правки
- **Фильтр на логгере не видит записей дочерних логгеров** — только на хендлере
- **Константа «число запросов в пути» считает фактический путь**, а не «с запасом».
  Обновляется в том шаге, где запрос добавляется
- **Замысел гварда важнее его буквального текста.** «Позиция по символу» — не
  «такая же позиция»; перепутать = путь открытия, закрывающий позиции
- **Кэш приватных данных аккаунта не смешивать с публичным market-data кэшем**
- **Фейки харнесса дрейфуют вместе с сигнатурами** и маскируют друг друга каскадом
- **Одна метрика — одна команда.** Две цифры ruff разошлись на два порядка
  из-за периметра, а не из-за кода

## Чего не делать

- Не деплоить и не применять миграции без явного «да» и свежего дампа
- **Деплой с миграцией** — другой порядок, чем no-op: собрать образ, бота не поднимать,
  `docker compose run --rm bot alembic upgrade head` новым образом, проверить схему,
  только потом `up -d`. Откат — сначала `alembic downgrade`, потом код из снапшота
- Разведка ничего не отправляет на биржу. Первый демо-ордер — отдельное «да»
- `.env` и `docker-compose.override.yml` не в git и не перезаписываются деплоем
- Не печатать `repr()` ответа внешнего API. Скан логов на секреты — только счётчики
- `telegram_id=424242` зарезервирован под `smoke_check.py`
- `build_fingerprint` не трогать — разошлёт повторные уведомления
- Шаг, зависящий от успеха предыдущего, — отдельным вызовом или через `&&`, не `;`
