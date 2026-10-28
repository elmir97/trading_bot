"""К7 (28.10.2026): дата в api.log. У api — время UTC с датой и явной меткой («2026-10-28
07:42:42Z», JSON «…+00:00»); у бота формат прежний («HH:MM:SS» местного времени процесса, без
даты) — на нём стоит чек-ап D (CLAUDE.md). Ротация и размер не менялись."""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.core import logging as app_logging
from app.core.logging import setup_logging

# 28.10.2026 07:42:42 UTC — в UTC+5 это 12:42:42: местное время и UTC различимы.
CREATED = 1793173362.0


@pytest.fixture(autouse=True)
def _restore_root() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for h in root.handlers:
        if h not in handlers:
            h.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def _line(tmp_path: Path, *, json_output: bool, **kw: Any) -> str:
    setup_logging(level="INFO", json_output=json_output, log_dir=str(tmp_path), **kw)
    root = logging.getLogger()
    buffer = io.StringIO()
    root.handlers[0].stream = buffer   # type: ignore[attr-defined]
    record = logging.LogRecord("probe", logging.INFO, "", 0, "событие", None, None)
    record.created = CREATED
    root.handle(record)
    return buffer.getvalue().rstrip("\n")


def test_bot_text_format_unchanged(tmp_path: Path) -> None:
    """Бот: «HH:MM:SS | LEVEL    | logger | message» местного времени, без даты — как до К7."""
    line = _line(tmp_path, json_output=False)
    local = time.strftime("%H:%M:%S", time.localtime(CREATED))
    assert line == f"{local} | INFO     | probe | событие"
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2} \| INFO {5}\| probe \| событие", line)
    # Не по удаче пояса машины (в контейнере UTC = местное): конвертер бота — местное время.
    assert all(h.formatter is not None and h.formatter.converter is time.localtime
               for h in logging.getLogger().handlers)


def test_api_text_has_utc_date_marked_z(tmp_path: Path) -> None:
    line = _line(tmp_path, json_output=False, log_file="api.log", utc_date=True)
    assert line == "2026-10-28 07:42:42Z | INFO     | probe | событие"
    # UTC — конвертером, не по совпадению пояса машины с UTC (контейнер).
    assert all(h.formatter is not None and h.formatter.converter is time.gmtime
               for h in logging.getLogger().handlers)


def test_api_json_ts_utc_explicit(tmp_path: Path) -> None:
    line = _line(tmp_path, json_output=True, log_file="api.log", utc_date=True)
    assert json.loads(line)["ts"] == "2026-10-28T07:42:42+00:00"
    assert all(h.formatter is not None and h.formatter.converter is time.gmtime
               for h in logging.getLogger().handlers)


def test_bot_json_ts_unchanged(tmp_path: Path) -> None:
    line = _line(tmp_path, json_output=True)
    expected = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(CREATED))
    assert json.loads(line)["ts"] == expected
    assert all(h.formatter is not None and h.formatter.converter is time.localtime
               for h in logging.getLogger().handlers)


def test_api_file_handler_same_format_and_rotation(tmp_path: Path) -> None:
    """Файл api.log — тот же формат с датой; ротация 10 МБ × 5, как у бота."""
    setup_logging(level="INFO", json_output=False, log_dir=str(tmp_path), log_file="api.log",
                  utc_date=True)
    [file_handler] = [h for h in logging.getLogger().handlers
                      if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert Path(file_handler.baseFilename) == tmp_path / "api.log"
    assert (file_handler.maxBytes, file_handler.backupCount) == (
        app_logging._LOG_FILE_MAX_BYTES, app_logging._LOG_FILE_BACKUP_COUNT)
    assert (file_handler.maxBytes, file_handler.backupCount) == (10 * 1024 * 1024, 5)
    record = logging.LogRecord("probe", logging.INFO, "", 0, "событие", None, None)
    record.created = CREATED
    logging.getLogger().handle(record)
    file_handler.flush()
    text = (tmp_path / "api.log").read_text(encoding="utf-8")
    assert text.startswith("2026-10-28 07:42:42Z | INFO     | probe | событие")


def test_utc_dates_sort_as_text(tmp_path: Path) -> None:
    """«После T0» — сравнение строкой (awk '$1 >= t'): фиксированная ширина, порядок времени."""
    fmt = logging.Formatter(datefmt=app_logging._UTC_DATEFMT)
    fmt.converter = time.gmtime
    stamps = []
    for created in (CREATED - 86400 + 1, CREATED, CREATED + 9, CREATED + 3600):
        r = logging.LogRecord("p", logging.INFO, "", 0, "m", None, None)
        r.created = created
        stamps.append(fmt.formatTime(r, fmt.datefmt))
    assert stamps == sorted(stamps) and len({len(s) for s in stamps}) == 1


async def test_api_lifespan_sets_up_utc_api_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Процесс api настраивает логи файлом api.log с датой UTC (бот — без, app/main.py)."""
    from app.api import main as api_main

    seen: dict[str, Any] = {}

    class StopLifespanError(Exception):
        pass

    def fake_setup(**kw: Any) -> None:
        seen.update(kw)

    def fake_build(settings: Any) -> Any:
        raise StopLifespanError

    monkeypatch.setattr(api_main, "setup_logging", fake_setup)
    monkeypatch.setattr(api_main, "build_state", fake_build)
    app = api_main.create_app()
    with pytest.raises(StopLifespanError):
        async with app.router.lifespan_context(app):
            pass
    assert seen["log_file"] == "api.log" and seen["utc_date"] is True


def test_bot_process_keeps_old_format() -> None:
    """app/main.py — без utc_date: bot.log без даты (чек-ап D)."""
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    call = src[src.index("setup_logging("):]
    call = call[:call.index(")") + 1]
    assert "utc_date" not in call and "log_file" not in call


def test_after_t_filter_needs_year_check() -> None:
    """Фильтр раздела D (CLAUDE.md): строки до К7 — «HH:MM:SS» без даты; строкой «21:00:00» >
    «2026-…», поэтому без проверки года старые строки 20–23 ч прошли бы «после T»."""
    t = "2026-10-28 07:00:00Z"
    lines = ["21:00:00 | INFO     | x | старый формат", "06:59:59 | INFO     | x | старый",
             "2026-10-28 06:59:59Z | INFO     | x | до T",
             "2026-10-28 07:00:01Z | INFO     | x | после"]
    first = [ln.split(" | ")[0] for ln in lines]
    naive = [f for f in first if f >= t]
    assert "21:00:00" in naive                                  # ловушка без проверки года
    checked = [f for f in first if re.match(r"^20[0-9][0-9]-", f) and f >= t]
    assert checked == ["2026-10-28 07:00:01Z"]
    claude = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    assert "$1 ~ /^20[0-9][0-9]-/ && $1 >= t" in claude
