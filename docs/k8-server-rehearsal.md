# К8: подготовка изолированной серверной репетиции

Локальная подготовка в `k8-net-r`, 30.10.2026. Это инструкция будущего запуска,
а не свидетельство серверной проверки и не разрешение на подключение/деплой.
К8 не включать в ноябрьский деплой `miniapp` на d2b082d.

Локальная проверка после исправления уборки: pytest 2733 passed = collect, 0 failed/0 skipped;
smoke 102/102 ×2, сеть 0; shellcheck и bash -n команд — exit 0;
мутации 13/13 CAUGHT, остальные исходы 0. Ruff 4975 и mypy 60 — существующий
долг без изменения к базе, оба exit 1. Отчёты:
logs/k8-cleanup-validation-20261030/accepted. Проверки PostgreSQL выполнены
на локальной 18.6 в trading_bot_test; Docker и сервер не использовались.
POSIX-права/fsync и контейнерная репетиция ещё требуют серверного запуска.

## Объём и защита

Только явный `--k8`, только 919ae8ad384e → c8a030202610, без rewind/fault и
без исключений данных, количества строк, схемы или blanket-проверок NULL.
Без `--k8` общий скрипт сохраняет прежнюю процедуру и удаление rehearsal-тега.
Общие --expect-columns/--expect-null в К8 отклоняются: проверки схемы и NULL
заданы самим контрактом К8, неподдерживаемые ожидания не игнорируются.

К8 требует уникальный тег `trading_bot:rehearsal_k8_<suffix>`, полный image ID
и SHA коммита. Проверяет соответствие тега ID и OCI revision label; затем
все запуски идут по image ID. Образ сохраняется как артефакт, автоматического
prune нет. Боевой latest, rollback-теги и работающие сервисы не меняются.
Создание боевого rollback-тега относится к будущему деплою, не к этой сборке.

Источник — только SELECT с default_transaction_read_only и pg_dump.
Копия — отдельный postgres:16-alpine в tmpfs, internal-сеть, без портов и
боевых томов. Совпадения имени образа недостаточно: server_version_num копии
должен точно совпасть с источником, включая minor. При расхождении — отказ,
автоматического обновления образов нет. Дамп не покидает сервер.

RAM available ≥ max(700, tmpfs_MB + 256 + 200), где tmpfs_MB=max(256, 3×DB_MB).
Диск ≥ 2×DB_MB + 200 МБ: дамп, архивы, запас. Это консервативный порог,
фактическую достаточность ресурсов и время операций подтверждает серверный
запуск. Копия сохраняет существующий лимит --memory 256m; OOM означает провал,
лимиты не увеличиваются автоматически. Перед сборкой отдельно available ≥700 МБ.

Мигратор запускается без .env/compose, только в сети копии, со свежими тестовыми
реквизитами. Для записи в root-owned каталог запуска он работает под UID/GID
вызывающего оператора (--user); это только одноразовый контейнер репетиции.
Бот/api не запускаются. На каждом соединении операции Python проверяет имя
БД trading_bot_rehearsal, фактический адрес сервера, точную версию PostgreSQL
и default search_path до любых изменений. lock_timeout=10s, statement_timeout=120s.
Локальный CLI `scripts.k8_rollback` по-прежнему допускает только loopback
и trading_bot_test. Обход через переменную разрешения продовой БД не добавлен.

## Архивы и проверки

Каталог /opt/backups должен принадлежать оператору, иметь 700 и не быть symlink.
Каждый запуск создаёт новый /opt/backups/rehearsal_k8_<UTC>_<random>/ (700).
В нём остаются rehearsal.log, rollback-1.json, rollback-2.json (при финальном
downgrade), synthetic-1.json, synthetic-2.json, соответствующие .sha256 и
k8-result.json. Файлы создаются при umask 077; JSON/sidecar эксклюзивно, 600,
с fsync файла и каталога. Архив проверяется чтением обратно до downgrade;
внутренний SHA256 payload дополняется внешним SHA256 целого файла. Перед restore
обе проверки повторяются, проверяются тип/права/владелец файла. Не переименовывать
архивы разных циклов как взаимозаменяемые. Архив не заменяет полный дамп.

На неизменённой копии:

1. Baseline после restore дампа: ревизия, схема, SHA256 содержимого каждой
   public-таблицы с детерминированной сортировкой и длинами строк.
2. Upgrade: старый RR точно скопирован в legacy (включая NULL), old=true;
   план остаётся только у eligible-строк; остальные данные неизменны.
3. Архивный downgrade: полные данные и схема равны baseline.
4. Upgrade → restore: схема равна первому upgrade; RR/legacy/флаги/отпечатки
   всех строк равны первому upgrade, остальные public-данные неизменны.
5. По умолчанию второй архивный downgrade с новым архивом; проверка baseline
   повторяется. --no-final-downgrade оставляет копию на К8 только до уборки.

Отдельная транзакционная синтетическая схема в той же копии проверяет старые
NULL и RR, открытый план, несколько ENTRY, EXIT, новые строки после К8 и между
downgrade/upgrade, повторный цикл; шесть конфликтов restore (NULL/другой план,
изменение/удаление сделки, изменение/удаление fill). Каждый конфликт должен
отказать без частичной записи. Схема откатывается целиком, реальные строки
копии для тестовых вставок не используются. Дополнительные локальные тесты
проверяют повреждения/формат/дубликаты ID архива, ошибки сохранения, неверную
БД, запрет перезаписи и сохранение ограничений локального CLI.

## Уборка и приёмка

Проверка ресурса имеет три исхода: есть, достоверно отсутствует, результат
неизвестен. Отсутствие подтверждается только успешным получением полного
списка соответствующего типа ресурсов Docker и отсутствием точного имени/тега
в нём. Ненулевой код inspect не доказывает отсутствие. Ошибка списка,
проверки метки, удаления или проверки после удаления означает неподтверждённую
уборку и exit 3, перекрывающий основной результат 0/1/2. При неизвестном
исходе до удаления удаление запрещено. Образ К8 сохраняется; обычный режим
по-прежнему удаляет временный rehearsal-тег, но теперь также требует
достоверного подтверждения уборки. Архивы не участвуют в удалении.

EXIT/INT/TERM/HUP вызывают уборку. Удаляются только контейнер мигратора,
контейнер копии и сеть с точной меткой rehearsal.run текущего запуска;
временный дамп — только по уникальному пути этого запуска. Чужие ресурсы и
старые архивы не удаляются. Образ, архивы и отчёт сохраняются. SIGKILL/сбой
Docker daemon нельзя обработать trap: остатки проверяются следующим запуском,
который отказывает; ручная уборка требует отдельного согласованного объёма.

Приёмка: exit 0, ИТОГ OK, все проверки данных/схем/архивов зелёные, временных
контейнеров/сети/дампа нет; сохранённый каталог и образ явно перечислены.
0 — успех, 2 — отказ предусловий, 1 — сбой/провал проверки, 3 — неполная уборка
(перекрывает остальные коды). При ошибке не чинить данные на ходу. Отсутствие
k8-result.json означает незавершённый цикл. Лог источника не является baseline:
боевые писатели могут продолжать работать во время согласованного дампа.

## Точные команды будущего запуска

Выполнять только после отдельного разрешения серверного объёма и свежего
чек-апа по CLAUDE.md. Перед сборкой показать выбранный коммит и команды.
Сначала локальная реализация должна быть принята и отдельно сохранена в Git:
текущий 0d29bbb этих изменений не содержит. Ниже Git Bash, не PowerShell.

```bash
set -euo pipefail
K8_COMMIT='<полный SHA будущего принятого коммита k8-net-r>'
test "$(git rev-parse "$K8_COMMIT")" = "$K8_COMMIT"
test -z "$(git status --porcelain)"
test "$(git branch --show-current)" = k8-net-r
IMAGE="trading_bot:rehearsal_k8_${K8_COMMIT:0:12}_$(date -u +%Y%m%d_%H%M%S)"
HOST=root@147.45.111.10

# Свежие резервные копии по регламенту; остаются на сервере, уборка их не трогает.
ssh "$HOST" bash -s -- "$IMAGE" <<'SH'
umask 077
set -euo pipefail
BACKUP="/opt/backups/k8_pre_rehearsal_${1##*:}"
test ! -L /opt/backups
test "$(stat -c %a /opt/backups)" = 700
mkdir -m 700 "$BACKUP"
docker exec -e 'PGOPTIONS=-c default_transaction_read_only=on' trading_bot_db \
  sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' | \
  gzip > "$BACKUP/source.sql.gz"
tar czf "$BACKUP/code.tar.gz" --exclude=trading_bot/logs -C /opt trading_bot
gzip -t "$BACKUP/source.sql.gz" "$BACKUP/code.tar.gz"
for item in "$BACKUP/source.sql.gz" "$BACKUP/code.tar.gz"; do
  test "$(stat -c %a "$item")" = 600
done
cd "$BACKUP"
sha256sum source.sql.gz code.tar.gz > SHA256SUMS
sha256sum -c SHA256SUMS
printf 'Backup directory: %s\n' "$BACKUP"
SH

# Только изолированная сборка; working tree сервера не распаковывается.
git -c core.autocrlf=false archive "$K8_COMMIT" | ssh "$HOST" \
  "umask 077; if docker image inspect '$IMAGE' >/dev/null 2>&1; then exit 2; fi; m=\$(free -m | awk '/^Mem:/ {print \$7}'); echo RAM_available_MB=\$m; test \"\$m\" -ge 700 && exec docker build --label org.opencontainers.image.revision='$K8_COMMIT' -t '$IMAGE' -"
IMAGE_ID=$(ssh "$HOST" "docker image inspect -f '{{.Id}}' '$IMAGE'")
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
printf '%s %s %s\n' "$K8_COMMIT" "$IMAGE" "$IMAGE_ID"

# Проверка файлов именно внутри нового образа, без распаковки боевого кода.
{
  git ls-tree -r --name-only "$K8_COMMIT" app alembic
  printf '%s\n' scripts/k8_rollback.py scripts/k8_rehearsal.py requirements.txt Dockerfile
} | while IFS= read -r path; do
  digest=$(git -c core.autocrlf=false show "$K8_COMMIT:$path" | md5sum | cut -d' ' -f1)
  printf '%s  %s\n' "$digest" "$path"
done | ssh "$HOST" "docker run --rm -i --network none '$IMAGE_ID' md5sum -c --quiet -"

# Offline SQL — из того же нового образа, без сети и продовых переменных.
ssh "$HOST" "docker run --rm --network none -e BOT_TOKEN=0:rehearsal -e DATABASE_URL=postgresql+asyncpg://rehearsal:unused@127.0.0.1/trading_bot_rehearsal '$IMAGE_ID' sh -c 'export ENCRYPTION_KEY=\$(python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"); exec alembic upgrade 919ae8ad384e:c8a030202610 --sql'"

# До этого шага сверить SQL и фактическую исходную ревизию: ровно M9.
# Иная ревизия/неожиданный SQL — стоп, план пересматривается.
git -c core.autocrlf=false show "$K8_COMMIT:scripts/rehearse_migration.sh" | \
  ssh "$HOST" bash -s -- --k8 --from 919ae8ad384e --to c8a030202610 \
    --image "$IMAGE" --image-id "$IMAGE_ID" --commit "$K8_COMMIT"
```

Полные md5 файлов сверяются с выбранным коммитом до запуска по регламенту;
OCI label сама по себе не доказывает содержимое образа. Сохранить локально
выбранные SHA, image ID и вывод offline SQL без секретов. Архивы БД/К8 остаются
на сервере; в отчёт достаточно итогов, количества строк, прав и SHA256 файлов.
Не выполнять docker compose, tag latest, restart/stop bot/api или миграцию
исходной БД в рамках этой процедуры. Удаление сохранённого образа и каталога
позже — отдельная явная операция, автоматической очистки по возрасту нет.

Будущий продовый откат требует отдельной процедуры и разрешения: остановка
обоих писателей bot/api, свежий полный дамп, проверенный новый архив, downgrade
новым образом, проверка ревизии и только затем возврат старого кода. Здесь
такой вход для продовой БД не реализован.
