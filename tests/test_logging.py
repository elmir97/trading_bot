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
