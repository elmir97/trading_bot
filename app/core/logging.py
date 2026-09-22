"""Настройка логирования.

Ключевая часть — SecretRedactingFilter. Он вырезает известные секреты из
любой строки лога, включая те, что попали туда через traceback стороннего
кода. Это последняя линия защиты: полагаться только на дисциплину
разработчика «не логировать токен» ненадёжно.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import orjson

_REDACTED = "***REDACTED***"

# 10 МБ × 5 файлов — не про объём (бот пишет на INFO не так много), а про
# то, чтобы цикл ошибок с потоком одинаковых записей не забил диск молча.
_LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
_LOG_FILE_BACKUP_COUNT = 5

# Единый источник "что считать полем extra={...}", а не отдельный список у
# каждого потребителя (SecretRedactingFilter/JsonFormatter/TextFormatter) —
# три копии одного и того же неизбежно разойдутся. "message"/"asctime" не
# из LogRecord.__init__, их пишет сам Formatter.format() как побочный
# эффект на record.__dict__ (и оба хендлера делят один и тот же record) —
# без явного исключения они попали бы в хвост как псевдо-extra.
_RESERVED_LOG_RECORD_KEYS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime"}


class SecretRedactingFilter(logging.Filter):
    """Заменяет секреты на плейсхолдер в сообщении и аргументах записи."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        # Короткие значения игнорируем: они дают ложные срабатывания.
        self._secrets = tuple(s for s in secrets if s and len(s) >= 8)

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._scrub(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a for a in record.args
                )

        # Traceback — главная лазейка. Секрет попадает туда не из нашего
        # кода, а из исключения сторонней библиотеки: httpx или aiogram
        # охотно включают URL с токеном в текст ошибки. Форматируем
        # traceback здесь и вычищаем, пока он ещё строка: logging
        # переиспользует готовый exc_text и повторно его не собирает.
        if record.exc_info and not record.exc_text:
            import traceback

            record.exc_text = self._scrub(
                "".join(traceback.format_exception(*record.exc_info))
            )
        elif record.exc_text:
            record.exc_text = self._scrub(record.exc_text)

        # Значения из extra={...} тоже уходят в вывод.
        for key, value in list(record.__dict__.items()):
            if key not in self._RESERVED_KEYS and isinstance(value, str):
                record.__dict__[key] = self._scrub(value)

        return True

    _RESERVED_KEYS = _RESERVED_LOG_RECORD_KEYS


class JsonFormatter(logging.Formatter):
    """Однострочный JSON — удобно грепать и скармливать в лог-коллектор."""

    _RESERVED = _RESERVED_LOG_RECORD_KEYS

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info or record.exc_text:
            # Берём exc_text, подготовленный фильтром: он уже очищен от
            # секретов. Собирать traceback заново здесь означало бы
            # обойти фильтр и вернуть утечку.
            payload["exception"] = record.exc_text or self.formatException(
                record.exc_info  # type: ignore[arg-type]
            )

        # Всё, что передали через extra={...}, попадает в структурированный вывод.
        for key, value in record.__dict__.items():
            if key not in self._RESERVED:
                payload[key] = value

        return orjson.dumps(payload, default=str).decode()


def _quote_if_needed(text: str) -> str:
    return f'"{text}"' if (" " in text or "=" in text) else text


def _format_extra(record: logging.LogRecord) -> str:
    """` | key=value key2=value2` для полей extra={...}, отсортировано по
    ключу — стабильно между запусками, не зависит от порядка вставки.
    Пусто, если extra не было."""
    extras = {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_LOG_RECORD_KEYS
    }
    if not extras:
        return ""
    rendered = " ".join(
        f"{key}={_quote_if_needed(str(extras[key]))}" for key in sorted(extras)
    )
    return f" | {rendered}"


class TextFormatter(logging.Formatter):
    """Человекочитаемый однострочный формат для LOG_JSON=false.

    Раздел 16 ТЗ, шаг 15.5.1а: базовый logging.Formatter не знает про
    extra={...} и просто теряет эти поля — на проде (LOG_JSON=false) это
    касалось всех строк с extra, не только новой про TTL лока. К 15.5.2
    это дорого: при сбое реальной отправки ордера лог — единственная
    запись о произошедшем. SecretRedactingFilter extra уже вычищает
    (запись в record.__dict__ до format(), см. filter() выше) — дописать
    сюда нужно было только рендер, не редактирование."""

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record) + _format_extra(record)


def setup_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
    secrets: tuple[str, ...] = (),
    log_dir: str = "logs",
    log_file: str = "bot.log",
) -> None:
    """Конфигурирует корневой логгер. Вызывать один раз при старте.

    Пишет и в stdout (докер собирает его в свой буфер, но тот не переживает
    пересоздание контейнера — теряется при каждом деплое), и в файл в
    смонтированной ./logs — та переживает пересборку образа, потому что
    живёт на хосте, а не внутри контейнера.
    """
    formatter = (
        JsonFormatter()
        if json_output
        else TextFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    # Один фильтр на оба хендлера: секрет не должен просочиться ни туда,
    # ни туда — а не только в тот вывод, который проверили при разработке.
    secret_filter = SecretRedactingFilter(secrets)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(secret_filter)

    handlers: list[logging.Handler] = [stdout_handler]

    try:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path / log_file,
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(secret_filter)
        handlers.append(file_handler)
    except OSError:
        # Директория не примонтирована или не пишется этим пользователем —
        # деградируем до stdout, а не роняем бота из-за логирования.
        logging.getLogger(__name__).warning(
            "Не удалось открыть файл логов %s — пишу только в stdout",
            Path(log_dir) / log_file,
        )

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)

    # Библиотеки шумят на INFO и могут вывести URL с параметрами.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
