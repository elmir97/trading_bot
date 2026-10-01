#!/usr/bin/env bash
# Репетиция миграции на копии прода одной командой.
#
# Запуск — только с Windows, закоммиченная версия через stdin (CLAUDE.md,
# «Репетиция миграции»): версия скрипта и проверяемой миграции — с одного
# коммита, копия в /opt/trading_bot/scripts — от прошлого деплоя.
#
#   git -c core.autocrlf=false show HEAD:scripts/rehearse_migration.sh \
#     | ssh root@147.45.111.10 bash -s -- --from <rev> --image trading_bot:rehearsal
#
# Прод только читает: pg_dump и SELECT (count, pg_database_size) в его
# контейнере Postgres, транзакции READ ONLY. Копия — тот же образ Postgres в
# tmpfs, в своей --internal-сети. Alembic — из образа нового кода, в той же
# сети, без .env и без compose: до базы прода он не дотянется даже по ошибке
# в URL. Перед каждым upgrade/downgrade тот же контейнер сверяет имя базы и
# адрес сервера с копией — иначе alembic не запускается.
#
# Защита от «съеденного stdin» (28.09 дважды): весь скрипт — определения
# функций, последняя строка `main "$@"; exit $?` — bash разбирает его целиком
# до первого шага; stdin процесса — /dev/null; docker — только через dk(),
# alembic — только через al(). Голый docker — лишь в restore-пайпе.
#
# Обрыв ssh не убивает уборку: SIGPIPE игнорируется, вывод дублируется в
# /opt/backups/rehearsal_<ts>.log (600), итог тогда читать в логе. Лог уборка
# не удаляет.
#
# Коды выхода: 0 — OK; 1 — провал миграции или проверки; 2 — отказ до
# первого изменения копии (аргументы, предусловия, ревизия копии ≠ --from);
# 3 — уборка неполная (перекрывает 0 и 1).

readonly PG_IMAGE=postgres:16-alpine
readonly BACKUP_DIR=/opt/backups
readonly PROD_DB_CONTAINER=trading_bot_db
readonly COPY_USER=rehearsal
readonly COPY_DB=trading_bot_rehearsal
readonly COPY_MEMORY=256m
readonly TMPFS_MIN_MB=256
readonly MIN_FREE_RAM_MB=512
readonly MIN_FREE_DISK_MB=200
readonly GUARD_FAIL_RC=97
readonly IMAGE_RE='^trading_bot:rehearsal[A-Za-z0-9_.-]*$'
readonly REV_RE='^[0-9A-Za-z_]+$'
readonly NAME_RE='^[A-Za-z0-9][A-Za-z0-9_.-]*$'
readonly IDENT_RE='^[A-Za-z0-9_]+$'
readonly IDENT2_RE='^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$'

dk() { docker "$@" </dev/null; }

# alembic в образе нового кода против копии. Проверка адреса и alembic — в
# одном контейнере с одним DATABASE_URL: разъехаться им нечем.
al() {
    local db=$COPY_DB
    if [[ $FAULT == wrong-db ]]; then db=postgres; fi
    # shellcheck disable=SC2016  # $GUARD_PY и $@ раскрывает sh в контейнере
    dk run --rm --network "$NET" \
        -e "DATABASE_URL=postgresql+asyncpg://$COPY_USER:$COPY_PW@$PG:5432/$db" \
        -e "BOT_TOKEN=0:rehearsal" -e "ENCRYPTION_KEY=$FERNET" \
        -e "EXPECT_DB=$COPY_DB" -e "EXPECT_ADDR=$COPY_ADDR" -e "GUARD_PY=$GUARD_PY" \
        "$IMAGE" sh -c 'python -c "$GUARD_PY" && exec alembic "$@"' sh "$@"
}

guard_py() {
    cat <<'PY'
import asyncio
import os
import sys

import asyncpg


async def main() -> int:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(url, timeout=10)
    try:
        db, addr = await conn.fetchrow(
            "SELECT current_database(), host(inet_server_addr())"
        )
    finally:
        await conn.close()
    ok = db == os.environ["EXPECT_DB"] and addr == os.environ["EXPECT_ADDR"]
    print(f"guard: db={db} addr={addr} {'ok' if ok else 'MISMATCH'}", flush=True)
    return 0 if ok else 97


sys.exit(asyncio.run(main()))
PY
}

# Ревизии по файлам образа, без базы: head, цепочка base → to.
meta_py() {
    cat <<'PY'
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory


def main(base: str, frm: str, to: str) -> str | None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = list(script.get_heads())
    print("heads=" + ",".join(heads))
    if len(heads) != 1:
        return "в образе несколько head: " + ", ".join(heads)
    head = heads[0]
    if to == "head":
        to = head
    if to != head:
        return f"--to {to} — не head образа ({head})"
    try:
        chain = [rev.revision for rev in script.iterate_revisions(to, base)]
    except Exception as exc:
        return f"нет цепочки {base} → {to}: {exc}"
    if not chain:
        return f"нечего репетировать: {base} == {to}"
    if frm != base and frm not in chain:
        return f"--from {frm} не лежит между {base} и {to}"
    print("to=" + to)
    print("chain=" + ",".join(reversed(chain)))
    return None


sys.exit(main(*sys.argv[1:4]))
PY
}

count_sql() {
    cat <<'SQL'
SELECT table_name || '|' || (xpath('/row/c/text()', query_to_xml(
           format('SELECT count(*) AS c FROM public.%I', table_name),
           false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY 1 COLLATE "C";
SQL
}

# Нормализованный снимок схемы: строка «вид|таблица|имя|определение»,
# порядок — по тексту (колонки по имени, не по attnum).
schema_sql() {
    cat <<'SQL'
SELECT line FROM (
    SELECT 'col|' || table_name || '|' || column_name || '|' || data_type
           || coalesce('(' || character_maximum_length || ')', '')
           || coalesce('(' || numeric_precision || ',' || numeric_scale || ')', '')
           || '|' || is_nullable || '|' || coalesce(column_default, '') AS line
    FROM information_schema.columns
    WHERE table_schema = 'public'
    UNION ALL
    SELECT 'idx|' || tablename || '|' || indexname || '|' || indexdef
    FROM pg_indexes
    WHERE schemaname = 'public'
    UNION ALL
    SELECT 'con|' || cl.relname || '|' || co.conname || '|' || co.contype
           || '|' || pg_get_constraintdef(co.oid)
    FROM pg_constraint co
    JOIN pg_class cl ON cl.oid = co.conrelid
    WHERE co.connamespace = 'public'::regnamespace
) s
ORDER BY line COLLATE "C";
SQL
}

usage() {
    cat <<'EOF'
Репетиция миграции на копии прода. Запуск — через stdin (CLAUDE.md):
  bash -s -- --from <rev> --image trading_bot:rehearsal[...] [опции]

  --from <rev>                  ревизия прода (alembic_version копии обязана совпасть)
  --to <rev>                    целевая ревизия; по умолчанию и обязательно — head образа
  --image <tag>                 образ нового кода, только trading_bot:rehearsal*
                                (тег в конце удаляется)
  --final-downgrade             второй downgrade — прогон отката (по умолчанию)
  --no-final-downgrade          без второго downgrade
  --expect-columns t.c[,t.c]    колонки, которые обязаны быть после upgrade
  --allow-count-change t[,t]    таблицы, где число строк вправе меняться
  --allow-schema-diff o[,o]     объекты схемы (таблица или таблица.имя), которым
                                разрешено расходиться при сверке после downgrade/upgrade
  --rewind-to <rev>             сначала опустить КОПИЮ до <rev>, репетировать <rev> → --to
                                (миграция, уже стоящая на проде)
  --source-container <name>     откуда дамп; по умолчанию trading_bot_db (прод)
  --fault wrong-db              проверка защиты: URL alembic ведёт в чужую базу; только
                                с --source-container не прода и без --rewind-to

Коды выхода: 0 OK, 1 FAIL, 2 отказ до изменения копии, 3 уборка неполная.
EOF
}

refuse_args() {
    echo "ОТКАЗ: $*" >&2
    echo "Справка: --help" >&2
    exit 2
}

init_state() {
    FROM='' TO=head IMAGE='' REWIND='' SRC=$PROD_DB_CONTAINER FAULT='' FINAL_DOWN=1
    EXPECT_COLS='' ALLOW_COUNT='' ALLOW_SCHEMA='' BASE=''
    TS='' LOG='' DUMP='' NET='' PG='' TEE_PID=''
    COPY_PW='' FERNET='' COPY_ADDR='' GUARD_PY='' META_PY='' COUNT_SQL='' SCHEMA_SQL=''
    TMPFS_MB=0 DB_MB=0
    NET_CREATED=0 PG_CREATED=0 DUMP_CREATED=0 IMAGE_CHECKED=0
    REASON='' RC=0 DONE=0 ERR_AT='' CUR_STEP='' STEP_T0=0
    AL_OUT='' GUARD_ADDR='' REV=''
    CHAIN=() STEPS=() SNAP_ORDER=()
    declare -gA COUNTS=() SCHEMA=()
}

check_list() { # значение regex имя-флага
    local item items
    IFS=, read -ra items <<<"$1"
    for item in "${items[@]}"; do
        [[ $item =~ $2 ]] || refuse_args "$3: «$item» — не того вида"
    done
}

parse_args() {
    local src_given=0 opt val
    while (($#)); do
        case $1 in
            --from | --to | --image | --rewind-to | --source-container | --fault | \
                --expect-columns | --allow-count-change | --allow-schema-diff)
                (($# >= 2)) || refuse_args "у $1 нет значения"
                opt=$1 val=$2
                shift 2
                case $opt in
                    --from) FROM=$val ;;
                    --to) TO=$val ;;
                    --image) IMAGE=$val ;;
                    --rewind-to) REWIND=$val ;;
                    --source-container) SRC=$val src_given=1 ;;
                    --fault) FAULT=$val ;;
                    --expect-columns) EXPECT_COLS+=${EXPECT_COLS:+,}$val ;;
                    --allow-count-change) ALLOW_COUNT+=${ALLOW_COUNT:+,}$val ;;
                    --allow-schema-diff) ALLOW_SCHEMA+=${ALLOW_SCHEMA:+,}$val ;;
                esac
                ;;
            --final-downgrade)
                FINAL_DOWN=1
                shift
                ;;
            --no-final-downgrade)
                FINAL_DOWN=0
                shift
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) refuse_args "неизвестный аргумент: $1" ;;
        esac
    done

    [[ -n $FROM ]] || refuse_args "нужен --from (ревизия прода)"
    [[ $FROM =~ $REV_RE ]] || refuse_args "--from: «$FROM» — не ревизия"
    [[ $TO =~ $REV_RE ]] || refuse_args "--to: «$TO» — не ревизия"
    [[ -n $IMAGE ]] || refuse_args "нужен --image"
    [[ $IMAGE =~ $IMAGE_RE ]] ||
        refuse_args "--image «$IMAGE»: только trading_bot:rehearsal* — тег в конце удаляется, боевой образ и rollback-теги трогать нельзя"
    if [[ -n $REWIND ]]; then
        [[ $REWIND =~ $REV_RE ]] || refuse_args "--rewind-to: «$REWIND» — не ревизия"
        [[ $REWIND != "$FROM" ]] || refuse_args "--rewind-to совпадает с --from — перематывать нечего"
    fi
    [[ $SRC =~ $NAME_RE ]] || refuse_args "--source-container: «$SRC» — не имя контейнера"
    [[ $SRC != tb_rehearsal_* ]] || refuse_args "--source-container tb_rehearsal_* — имена самого скрипта"
    if [[ -n $FAULT ]]; then
        [[ $FAULT == wrong-db ]] || refuse_args "--fault: известен только wrong-db"
        if ((!src_given)) || [[ $SRC == "$PROD_DB_CONTAINER" ]]; then
            refuse_args "--fault — только с --source-container не прода"
        fi
        [[ -z $REWIND ]] || refuse_args "--fault несовместим с --rewind-to"
    fi
    check_list "$EXPECT_COLS" "$IDENT2_RE" --expect-columns
    check_list "$ALLOW_COUNT" "$IDENT_RE" --allow-count-change
    local item items
    IFS=, read -ra items <<<"$ALLOW_SCHEMA"
    for item in "${items[@]}"; do
        [[ $item =~ $IDENT_RE || $item =~ $IDENT2_RE ]] ||
            refuse_args "--allow-schema-diff: «$item» — нужна таблица или таблица.имя"
    done
    BASE=${REWIND:-$FROM}
}

start_log() {
    [[ -d $BACKUP_DIR ]] || refuse_args "нет каталога $BACKUP_DIR"
    TS=$(date -u +%Y%m%d_%H%M%S)
    LOG=$BACKUP_DIR/rehearsal_$TS.log
    DUMP=$BACKUP_DIR/rehearsal_$TS.sql.gz
    NET=tb_rehearsal_net_$TS
    PG=tb_rehearsal_db_$TS
    [[ ! -e $LOG ]] || refuse_args "$LOG уже есть — повторить через секунду"
    exec > >(tee --output-error=warn-nopipe -a "$LOG") 2>&1
    TEE_PID=$!
}

fail() {
    REASON=$*
    RC=1
    exit 1
}

refuse() {
    REASON=$*
    RC=2
    exit 2
}

# shellcheck disable=SC2329  # вызывается из trap
on_signal() {
    REASON=${REASON:-"прерван сигналом $1"}
    RC=1
    exit 1
}

indent() {
    # shellcheck disable=SC2001  # префикс каждой строки многострочного вывода
    sed 's/^/    /' <<<"$1"
}

begin_step() {
    CUR_STEP=$1
    STEP_T0=$SECONDS
    echo
    echo "== $((${#STEPS[@]} + 1)). $1"
}

end_step() { # ревизия проверки
    STEPS+=("| $((${#STEPS[@]} + 1)) | $CUR_STEP | ${1:-—} | ${2:-—} | ✅ | $((SECONDS - STEP_T0)) с |")
    CUR_STEP=''
}

src_sql() {
    # shellcheck disable=SC2016  # $SQL и $POSTGRES_* раскрывает sh в контейнере
    dk exec -e "SQL=$1" -e "PGOPTIONS=-c default_transaction_read_only=on" "$SRC" \
        sh -c 'psql -X -A -t -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$SQL"'
}

copy_sql() {
    # shellcheck disable=SC2016
    dk exec -e "SQL=$1" "$PG" \
        sh -c 'psql -X -A -t -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$SQL"'
}

# В REV, а не через $(...): fail в подоболочке потерял бы причину.
copy_revision() {
    REV=$(copy_sql "SELECT version_num FROM alembic_version") || fail "не прочитать alembic_version копии"
}

snap() {
    COUNTS[$1]=$(copy_sql "$COUNT_SQL") || fail "число строк ($1) не прочитано"
    SCHEMA[$1]=$(copy_sql "$SCHEMA_SQL") || fail "схема ($1) не прочитана"
    SNAP_ORDER+=("$1")
}

counts_mismatch() { # эталон снимок -> «таблица a→b; …» по недопущенным
    LC_ALL=C join -t'|' -a1 -a2 -e '—' -o 0,1.2,2.2 \
        <(LC_ALL=C sort <<<"${COUNTS[$1]}") <(LC_ALL=C sort <<<"${COUNTS[$2]}") |
        awk -F'|' -v allow=",$ALLOW_COUNT," '
            $2 != $3 && index(allow, "," $1 ",") == 0 { printf "%s%s %s→%s", sep, $1, $2, $3; sep = "; " }'
}

check_counts() {
    local bad
    bad=$(counts_mismatch baseline "$1") || fail "сверка числа строк ($1) не выполнилась"
    [[ -z $bad ]] || fail "число строк ($1) ≠ baseline: $bad"
}

schema_of() {
    awk -F'|' -v allow=",$ALLOW_SCHEMA," '
        index(allow, "," $2 ",") == 0 && index(allow, "," $2 "." $3 ",") == 0' <<<"${SCHEMA[$1]}"
}

check_schema() { # снимок эталон
    local d
    d=$(diff <(schema_of "$2") <(schema_of "$1")) && return 0
    indent "$d"
    fail "схема ($1) ≠ $2 (diff выше)"
}

check_expected() {
    local item items
    IFS=, read -ra items <<<"$EXPECT_COLS"
    for item in "${items[@]}"; do
        grep -q "^col|${item%%.*}|${item#*.}|" <<<"${SCHEMA[$1]}" || fail "нет колонки $item после $1"
    done
}

run_al() {
    local rc=0
    AL_OUT=$(al "$@" 2>&1) || rc=$?
    indent "$AL_OUT"
    GUARD_ADDR=$(awk '/^guard: / { sub(/^.*addr=/, ""); print $1; exit }' <<<"$AL_OUT")
    if ((rc == GUARD_FAIL_RC)); then
        fail "защита адреса: $(grep -m1 '^guard: ' <<<"$AL_OUT") — ожидалось db=$COPY_DB addr=$COPY_ADDR; $1 не запускался"
    fi
    ((rc == 0)) || fail "$1 $2 — код $rc (вывод выше)"
}

step_preconditions() {
    begin_step "Предусловия"
    echo "--from $FROM${REWIND:+, --rewind-to $REWIND}, --to $TO, образ $IMAGE, источник $SRC"
    echo "второй downgrade: $( ((FINAL_DOWN)) && echo да || echo нет)${FAULT:+, FAULT $FAULT}"
    local c
    for c in gzip zcat free df stat tee awk join sort diff du; do
        command -v "$c" >/dev/null || refuse "нет команды $c"
    done
    dk version --format '{{.Server.Version}}' >/dev/null 2>&1 || refuse "docker недоступен"

    local containers networks dumps left
    containers=$(dk ps -a --filter name=tb_rehearsal_ --format '{{.Names}}') || refuse "не прочитать список контейнеров"
    networks=$(dk network ls --filter name=tb_rehearsal_ --format '{{.Name}}') || refuse "не прочитать список сетей"
    dumps=$(compgen -G "$BACKUP_DIR/rehearsal_*.sql.gz" || true)
    left=$(printf '%s\n' "$containers" "$networks" "$dumps" | grep . | tr '\n' ' ' || true)
    [[ -z $left ]] || refuse "остатки прошлой репетиции (скрипт их не трогает): $left"

    local running src_image
    running=$(dk inspect -f '{{.State.Running}}' "$SRC" 2>/dev/null) || refuse "нет контейнера $SRC"
    [[ $running == true ]] || refuse "$SRC не запущен"
    src_image=$(dk inspect -f '{{.Config.Image}}' "$SRC") || refuse "не прочитать образ $SRC"
    [[ $src_image == "$PG_IMAGE" ]] ||
        refuse "$SRC на образе $src_image, копия — $PG_IMAGE: обновить PG_IMAGE вместе с docker-compose.yml"

    local ram
    ram=$(free -m | awk '/^Mem:/ { print $7 }')
    ((ram >= MIN_FREE_RAM_MB)) || refuse "свободно RAM $ram МБ < $MIN_FREE_RAM_MB МБ"

    dk image inspect "$IMAGE" >/dev/null 2>&1 || refuse "нет образа $IMAGE — сначала собрать (CLAUDE.md)"
    IMAGE_CHECKED=1
    local image_info meta
    image_info=$(dk image inspect -f '{{slice .Id 7 19}}, собран {{.Created}}' "$IMAGE") ||
        refuse "не прочитать образ $IMAGE"
    if ! meta=$(dk run --rm --network none "$IMAGE" python -c "$META_PY" "$BASE" "$FROM" "$TO" 2>&1); then
        indent "$meta"
        refuse "ревизии не сходятся с образом (вывод выше)"
    fi
    indent "$meta"
    TO=$(sed -n 's/^to=//p' <<<"$meta")
    IFS=, read -ra CHAIN <<<"$(sed -n 's/^chain=//p' <<<"$meta")"
    ((${#CHAIN[@]})) || refuse "цепочка ревизий не прочитана"

    local size disk
    size=$(src_sql "SELECT pg_database_size(current_database())") || refuse "не прочитать размер базы $SRC"
    DB_MB=$(((size + 1048575) / 1048576))
    TMPFS_MB=$((DB_MB * 3 > TMPFS_MIN_MB ? DB_MB * 3 : TMPFS_MIN_MB))
    disk=$(df -Pm "$BACKUP_DIR" | awk 'NR == 2 { print $4 }')
    ((disk >= DB_MB + MIN_FREE_DISK_MB)) || refuse "свободно на диске $disk МБ < $((DB_MB + MIN_FREE_DISK_MB)) МБ"

    end_step "" "RAM $ram МБ, диск $disk МБ, база $DB_MB МБ → tmpfs $TMPFS_MB МБ; образ $image_info; цепочка $BASE → ${CHAIN[*]}"
}

step_dump() {
    begin_step "Дамп $SRC"
    COUNTS["прод (инфо)"]=$(src_sql "$COUNT_SQL") || fail "число строк $SRC не прочитано"
    SNAP_ORDER+=("прод (инфо)")
    DUMP_CREATED=1
    # shellcheck disable=SC2016
    dk exec "$SRC" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' |
        gzip >"$DUMP" || fail "pg_dump"
    gzip -t "$DUMP" || fail "gzip -t $DUMP"
    local mode
    mode=$(stat -c %a "$DUMP")
    [[ $mode == 600 ]] || fail "режим дампа $mode, не 600"
    end_step "" "$DUMP, $(du -h "$DUMP" | cut -f1), режим 600, gzip -t ✅"
}

step_copy() {
    begin_step "Копия: $PG_IMAGE, tmpfs $TMPFS_MB МБ"
    NET_CREATED=1
    dk network create --internal "$NET" >/dev/null || fail "сеть $NET не создана"
    PG_CREATED=1
    dk run -d --name "$PG" --network "$NET" --memory "$COPY_MEMORY" \
        --tmpfs "/var/lib/postgresql/data:rw,size=${TMPFS_MB}m" \
        -e "POSTGRES_USER=$COPY_USER" -e "POSTGRES_PASSWORD=$COPY_PW" -e "POSTGRES_DB=$COPY_DB" \
        "$PG_IMAGE" -c fsync=off -c full_page_writes=off >/dev/null || fail "копия не запустилась"
    # По TCP, не по сокету: временный сервер initdb слушает только сокет и
    # отвечал бы pg_isready до перезапуска.
    local i
    for ((i = 0; i < 60; i++)); do
        if dk exec "$PG" pg_isready -q -h 127.0.0.1 -U "$COPY_USER" -d "$COPY_DB" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    ((i < 60)) || fail "копия не поднялась за 60 с"
    COPY_ADDR=$(dk inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$PG") ||
        fail "адрес копии не прочитан"
    [[ $COPY_ADDR =~ ^[0-9.]+$ ]] || fail "адрес копии не прочитан: «$COPY_ADDR»"
    end_step "" "сеть $NET (--internal), адрес $COPY_ADDR, --memory $COPY_MEMORY, fsync=off, full_page_writes=off"
}

step_restore() {
    begin_step "Restore дампа в копию"
    # shellcheck disable=SC2016
    zcat "$DUMP" | docker exec -i "$PG" sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null ||
        fail "restore (psql ON_ERROR_STOP)"
    end_step "" "ON_ERROR_STOP=1"
}

step_baseline() {
    begin_step "Ревизия копии"
    copy_revision
    [[ $REV == "$FROM" ]] || refuse "ревизия копии (= прода) $REV, а --from $FROM"
    end_step "$REV" "= --from"

    if [[ -n $REWIND ]]; then
        begin_step "Перемотка копии → $REWIND"
        run_al downgrade "$REWIND"
        copy_revision
        [[ $REV == "$REWIND" ]] || fail "после перемотки ревизия $REV, ожидалась $REWIND"
        end_step "$REV" "адрес $GUARD_ADDR ✅"
    fi

    begin_step "Baseline"
    snap baseline
    end_step "$REV" "таблиц $(grep -c . <<<"${COUNTS[baseline]}"), объектов схемы $(grep -c . <<<"${SCHEMA[baseline]}")"
}

migrate_step() { # upgrade|downgrade цель снимок эталон-схемы
    local dir=$1 target=$2 label=$3 ref=$4 word n checks
    begin_step "$dir → $target"
    run_al "$dir" "$target"
    if [[ $dir == upgrade ]]; then word="Running upgrade"; else word="Running downgrade"; fi
    n=$(grep -c "$word" <<<"$AL_OUT" || true)
    ((n == ${#CHAIN[@]})) || fail "строк «$word» $n, ожидалось ${#CHAIN[@]}"
    copy_revision
    [[ $REV == "$target" ]] || fail "ревизия копии $REV, ожидалась $target"
    snap "$label"
    check_counts "$label"
    checks="адрес $GUARD_ADDR ✅, «$word» ×$n, строки = baseline${ALLOW_COUNT:+ (кроме $ALLOW_COUNT)}"
    if [[ -n $ref ]]; then
        check_schema "$label" "$ref"
        checks+=", схема = $ref${ALLOW_SCHEMA:+ (кроме $ALLOW_SCHEMA)}"
    fi
    if [[ $dir == upgrade && -n $EXPECT_COLS ]]; then
        check_expected "$label"
        checks+=", колонки $EXPECT_COLS ✅"
    fi
    end_step "$REV" "$checks"
}

# shellcheck disable=SC2329  # из on_exit (trap)
cleanup() {
    echo
    echo "== Уборка"
    local left=()
    if ((PG_CREATED)); then
        dk rm -f "$PG" >/dev/null 2>&1 || true
        if dk container inspect "$PG" >/dev/null 2>&1; then left+=("контейнер $PG"); fi
    fi
    if ((NET_CREATED)); then
        dk network rm "$NET" >/dev/null 2>&1 || true
        if dk network inspect "$NET" >/dev/null 2>&1; then left+=("сеть $NET"); fi
    fi
    if ((IMAGE_CHECKED)); then
        dk rmi "$IMAGE" >/dev/null 2>&1 || true
        if dk image inspect "$IMAGE" >/dev/null 2>&1; then left+=("образ $IMAGE"); fi
    fi
    if ((DUMP_CREATED)); then
        rm -f "$DUMP" 2>/dev/null || true
        if [[ -e $DUMP ]]; then left+=("дамп $DUMP"); fi
    fi
    if ((${#left[@]})); then
        STEPS+=("| $((${#STEPS[@]} + 1)) | Уборка | — | осталось: ${left[*]} | ❌ | — |")
        REASON="${REASON:+$REASON; }уборка неполная: ${left[*]}"
        RC=3
    else
        STEPS+=("| $((${#STEPS[@]} + 1)) | Уборка | — | контейнер, сеть, тег, дамп — нет; лог оставлен | ✅ | — |")
    fi
}

# shellcheck disable=SC2329  # из on_exit (trap)
render_counts() {
    local l t n base_n mark line hdr="| таблица |" sep="|---|" tables
    for l in "${SNAP_ORDER[@]}"; do
        hdr+=" $l |"
        sep+="---|"
    done
    echo "$hdr"
    echo "$sep"
    tables=$(for l in "${SNAP_ORDER[@]}"; do printf '%s\n' "${COUNTS[$l]}"; done | cut -d'|' -f1 | LC_ALL=C sort -u)
    while read -r t; do
        [[ -n $t ]] || continue
        line="| $t |"
        base_n=$(count_of baseline "$t")
        for l in "${SNAP_ORDER[@]}"; do
            n=$(count_of "$l" "$t")
            mark=''
            if [[ $l != "прод (инфо)" && $l != baseline && $n != "$base_n" ]]; then
                if [[ ,$ALLOW_COUNT, == *",$t,"* ]]; then mark=' ⚠ допущено'; else mark=' ❌'; fi
            fi
            line+=" $n$mark |"
        done
        echo "$line"
    done <<<"$tables"
}

# shellcheck disable=SC2329  # из on_exit (trap)
count_of() {
    awk -F'|' -v t="$2" '$1 == t { print $2; f = 1 } END { if (!f) print "—" }' <<<"${COUNTS[$1]-}"
}

# shellcheck disable=SC2329  # из on_exit (trap)
report() {
    echo
    echo "== Отчёт: $BASE → $TO, образ $IMAGE, источник $SRC"
    echo
    echo "| № | шаг | ревизия после | проверки | итог | время |"
    echo "|---|---|---|---|---|---|"
    if ((${#STEPS[@]})); then printf '%s\n' "${STEPS[@]}"; fi
    if ((${#SNAP_ORDER[@]})); then
        echo
        echo "Число строк (прод — для информации, не сверяется):"
        echo
        render_counts
    fi
    if [[ -n ${SCHEMA[baseline]-} && -n ${SCHEMA[upgrade 1]-} ]]; then
        echo
        echo "Схема baseline → upgrade 1 (сверить с офлайн-SQL):"
        local d
        d=$(diff <(printf '%s\n' "${SCHEMA[baseline]}") <(printf '%s\n' "${SCHEMA[upgrade 1]}") |
            sed -n 's/^< /  - /p; s/^> /  + /p')
        echo "${d:-  (без изменений)}"
    fi
    echo
    echo "Лог: $LOG (режим $(stat -c %a "$LOG" 2>/dev/null), уборка не удаляет)"
}

# shellcheck disable=SC2329  # вызывается из trap
on_exit() {
    local code=$?
    trap '' INT TERM HUP
    set +e
    if [[ -z $REASON ]]; then
        if ((DONE)); then
            RC=0
        else
            REASON="оборвался, код $code${ERR_AT:+ ($ERR_AT)}"
            RC=1
        fi
    fi
    if [[ -n $CUR_STEP ]]; then
        STEPS+=("| $((${#STEPS[@]} + 1)) | $CUR_STEP | — | $REASON | ❌ | $((SECONDS - STEP_T0)) с |")
        CUR_STEP=''
    fi
    cleanup
    report
    echo
    if ((RC == 0)); then
        echo "ИТОГ: OK"
    else
        echo "ИТОГ: FAIL (код $RC: $REASON)"
    fi
    # Закрыть пайп в tee и дождаться: хвост лога дописан до выхода.
    exec 1>&- 2>&-
    wait "$TEE_PID" 2>/dev/null
    exit "$RC"
}

main() {
    trap '' PIPE
    exec </dev/null
    umask 077
    set -Eeuo pipefail
    init_state
    parse_args "$@"
    start_log
    trap 'ERR_AT="строка $LINENO: $BASH_COMMAND"' ERR
    trap on_exit EXIT
    trap 'on_signal INT' INT
    trap 'on_signal TERM' TERM
    trap 'on_signal HUP' HUP

    GUARD_PY=$(guard_py)
    META_PY=$(meta_py)
    COUNT_SQL=$(count_sql)
    SCHEMA_SQL=$(schema_sql)
    COPY_PW=$(head -c 18 /dev/urandom | base64 | tr -d '+/=')
    FERNET=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')

    step_preconditions
    step_dump
    step_copy
    step_restore
    step_baseline
    migrate_step upgrade "$TO" "upgrade 1" ""
    migrate_step downgrade "$BASE" "downgrade 1" baseline
    migrate_step upgrade "$TO" "upgrade 2" "upgrade 1"
    if ((FINAL_DOWN)); then
        migrate_step downgrade "$BASE" "downgrade 2" baseline
    fi
    DONE=1
}

main "$@"; exit $?
