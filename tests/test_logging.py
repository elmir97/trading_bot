"""Тесты фильтра секретов.

Регрессия здесь означает утечку токена бота в лог — поэтому проверяем
и прямое сообщение, и подстановку через args, и traceback.
"""

from __future__ import annotations

import logging

import pytest

from app.core.logging import SecretRedactingFilter

TOKEN = "1234567890:AAFakeBotTokenValueForTests"


def _record(msg: str, args: object = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg=msg, args=args, exc_info=None,
    )


def test_redacts_secret_in_message() -> None:
    record = _record(f"Запрос с токеном {TOKEN}")
    SecretRedactingFilter((TOKEN,)).filter(record)
    assert TOKEN not in record.getMessage()
    assert "REDACTED" in record.getMessage()


def test_redacts_secret_in_args() -> None:
    record = _record("Токен: %s", (TOKEN,))
    SecretRedactingFilter((TOKEN,)).filter(record)
    assert TOKEN not in record.getMessage()


def test_keeps_unrelated_text() -> None:
    record = _record("Обычное сообщение без секретов")
    SecretRedactingFilter((TOKEN,)).filter(record)
    assert record.getMessage() == "Обычное сообщение без секретов"


def test_ignores_short_values() -> None:
    """Короткие строки не редактируем: 'dev' вырезало бы полтекста логов."""
    record = _record("environment=dev")
    SecretRedactingFilter(("dev",)).filter(record)
    assert record.getMessage() == "environment=dev"


def test_filter_always_passes_record_through() -> None:
    record = _record(TOKEN)
    assert SecretRedactingFilter((TOKEN,)).filter(record) is True


class TestTracebackRedaction:
    """Регрессия: секрет утекал через текст исключения.

    Фильтр чистил сообщение и аргументы, но не traceback. А именно там
    токен и оказывается на практике: httpx и aiogram включают URL с
    токеном в текст своих исключений. Утечка происходила не из нашего
    кода — что и было главным доводом за существование фильтра.
    """

    @staticmethod
    def _capture(json_output: bool) -> str:
        import io
        import logging as std_logging

        from app.core.logging import setup_logging

        buffer = io.StringIO()
        setup_logging(level="INFO", json_output=json_output, secrets=(TOKEN,))
        std_logging.getLogger().handlers[0].stream = buffer

        log = std_logging.getLogger("probe")
        try:
            raise RuntimeError(
                f"request to https://api.telegram.org/bot{TOKEN}/getUpdates failed"
            )
        except RuntimeError:
            log.exception("сбой стороннего кода")
        return buffer.getvalue()

    def test_json_output(self) -> None:
        output = self._capture(json_output=True)
        assert TOKEN not in output
        assert "REDACTED" in output

    def test_plain_output(self) -> None:
        output = self._capture(json_output=False)
        assert TOKEN not in output

    def test_extra_fields_are_scrubbed(self) -> None:
        """Значения из extra={...} тоже попадают в вывод."""
        import io
        import logging as std_logging

        from app.core.logging import setup_logging

        buffer = io.StringIO()
        setup_logging(level="INFO", json_output=True, secrets=(TOKEN,))
        std_logging.getLogger().handlers[0].stream = buffer

        std_logging.getLogger("probe").info(
            "запрос", extra={"url": f"https://api.telegram.org/bot{TOKEN}/getMe"}
        )
        assert TOKEN not in buffer.getvalue()


class TestTextFormatterExtra:
    """Раздел 16 ТЗ, шаг 15.5.1а: до правки TextFormatter не существовал —
    extra={...} в текстовом режиме (LOG_JSON=false, боевой конфиг) терялся
    молча, потому что обычный logging.Formatter про extra не знает."""

    @staticmethod
    def _capture(**extra: object) -> str:
        import io
        import logging as std_logging

        from app.core.logging import setup_logging

        buffer = io.StringIO()
        setup_logging(level="INFO", json_output=False, secrets=(TOKEN,))
        std_logging.getLogger().handlers[0].stream = buffer

        std_logging.getLogger("probe").info("событие", extra=extra)
        return buffer.getvalue()

    def test_extra_field_appears_as_key_value(self) -> None:
        output = self._capture(confirm_lock_ttl_seconds=80)
        assert "confirm_lock_ttl_seconds=80" in output

    def test_no_extra_means_no_suffix(self) -> None:
        output = self._capture()
        assert output.strip().endswith("событие")

    def test_multiple_extra_fields_sorted_by_key(self) -> None:
        output = self._capture(zeta=1, alpha=2)
        # Порядок вставки — zeta раньше alpha; в выводе должен быть
        # алфавитный, иначе формат нестабилен между вызовами.
        assert output.index("alpha=2") < output.index("zeta=1")

    def test_extra_secret_is_redacted_in_rendered_line(self) -> None:
        """Секрет в extra доходит до текстового вывода уже вычищенным —
        SecretRedactingFilter мутирует record.__dict__ до format()."""
        output = self._capture(url=f"https://api.telegram.org/bot{TOKEN}/getMe")
        assert "url=" in output
        assert TOKEN not in output
        assert "REDACTED" in output

    def test_message_and_asctime_are_not_duplicated_as_extra(self) -> None:
        """Formatter.format() сам пишет record.message/record.asctime —
        без явного исключения они попали бы в хвост вторым разом."""
        output = self._capture()
        assert output.count("message=") == 0
        assert output.count("asctime=") == 0


class TestExtraKeysAreSafe:
    """Регрессия: logging роняет вызов, если extra перекрывает поля LogRecord.

    Ошибка вида extra={"created": ...} не видна при чтении кода и не
    ловится тестами модуля — она проявляется только в момент записи в
    лог, роняя тот хендлер, который пытался что-то залогировать.
    """

    def test_no_reserved_names_in_extra_across_project(self) -> None:
        import logging as std_logging
        import pathlib
        import re

        reserved = set(
            std_logging.LogRecord("", 0, "", 0, "", None, None).__dict__
        ) | {"message", "asctime"}

        collisions: list[str] = []
        app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"

        for path in app_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"extra=\{([^}]*)\}", text, re.S):
                for key in re.findall(r'"(\w+)"\s*:', match.group(1)):
                    if key in reserved:
                        line = text[: match.start()].count("\n") + 1
                        collisions.append(f"{path.name}:{line} → {key}")

        assert not collisions, "Зарезервированные имена в extra: " + ", ".join(
            collisions
        )

    def test_reserved_key_actually_breaks_logging(self) -> None:
        """Подтверждение, что проверка выше сторожит реальную проблему."""
        import logging as std_logging

        with pytest.raises(KeyError):
            std_logging.getLogger("probe").info("тест", extra={"created": 1})
