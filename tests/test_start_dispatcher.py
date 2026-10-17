"""/start и экран позиций на настоящих апдейтах (разбор 17.10).

Владелец набрал «/start» и получил экран «💼 Позиции» без заметки. Разбор: голый
/start — меню; экран позиций без заметки бот присылает только на /open и на кнопку
(«💼 Позиции» в меню правит сообщение меню); /start pos-… — экран с заметкой
«Позиции … уже нет». Чтобы в следующий раз ответил лог: INFO «Команда /start»
(вид параметра, не текст) и «Экран позиций» (что вызвало).

Настоящий build_dispatcher из app/main.py со всеми middleware — в отдельном
процессе: модульный Router подключается к диспетчеру один раз за процесс.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = r'''
import asyncio, json, logging, sys
from datetime import UTC, datetime

import fakeredis.aioredis
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser
from sqlalchemy import text

from app import main as bot_main
from app.bot.handlers import positions
from app.bot.keyboards.main import MenuCallback
from app.core.config import Settings
from app.database.session import Database

TID = int(sys.argv[1])
sent, loads, logs = [], [], []


class Collect(logging.Handler):
    def emit(self, record):
        if record.getMessage() in ("Команда /start", "Экран позиций"):
            logs.append([record.getMessage(), getattr(record, "param", None),
                         getattr(record, "trigger", None)])


for name in ("app.bot.handlers.common", "app.bot.handlers.positions"):
    logging.getLogger(name).addHandler(Collect())
    logging.getLogger(name).setLevel(logging.INFO)


async def fake_call(self, method, request_timeout=None):
    text_ = getattr(method, "text", None)
    if text_ is not None:
        sent.append([type(method).__name__, text_])
        return Message(message_id=500 + len(sent), date=datetime.now(UTC),
                       chat=Chat(id=TID, type="private"), text=text_)
    return True


async def fake_load(session, user, settings, cipher):
    loads.append(1)
    return [], None, [], {}


async def main():
    Bot.__call__ = fake_call
    positions._load = fake_load
    settings = Settings(trading_execution_enabled=True, allowed_telegram_ids=str(TID))
    db = Database(settings)
    dp = bot_main.build_dispatcher(settings, db, None, fakeredis.aioredis.FakeRedis())
    bot = Bot(token="999:AAFakeTokenForDispatcherTests")
    who = TgUser(id=TID, is_bot=False, first_name="t")
    chat = Chat(id=TID, type="private")
    out = {}
    cases = ["/start", "/start pos-XRP-USDT-L", "/start мусор", "/open",
             "cb:" + MenuCallback.OPEN_POSITIONS]
    for i, case in enumerate(cases, 1):
        sent.clear(); loads.clear(); logs.clear()
        if case.startswith("cb:"):
            menu = Message(message_id=400, date=datetime.now(UTC), chat=chat, text="меню")
            update = Update(update_id=i, callback_query=CallbackQuery(
                id=str(i), from_user=who, chat_instance="x", message=menu, data=case[3:]))
        else:
            length = len(case.split()[0])
            update = Update(update_id=i, message=Message(
                message_id=i, date=datetime.now(UTC), chat=chat, from_user=who, text=case,
                entities=[{"type": "bot_command", "offset": 0, "length": length}]))
        await dp.feed_update(bot=bot, update=update)
        out[case] = {"sent": list(sent), "loads": len(loads), "logs": list(logs)}
    async with db.engine.begin() as conn:
        await conn.execute(text("delete from users where telegram_id = :t"), {"t": TID})
    await bot.session.close()
    await db.dispose()
    print("RESULT " + json.dumps(out, ensure_ascii=False))


asyncio.run(main())
'''


@pytest.fixture(scope="module")
def run(request):  # type: ignore[no-untyped-def]
    if not os.getenv("DATABASE_URL"):
        pytest.skip("Нужен PostgreSQL")
    tid = 990_000_000 + os.getpid() % 100_000
    done = subprocess.run([sys.executable, "-c", _SCRIPT, str(tid)], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8",
                          stdin=subprocess.DEVNULL, check=False)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert done.returncode == 0 and line, done.stdout[-2000:] + done.stderr[-3000:]
    return json.loads(line[len("RESULT "):])


def _texts(result: dict) -> list[str]:  # type: ignore[type-arg]
    return [text for _, text in result["sent"]]


def test_bare_start_is_main_menu(run) -> None:  # type: ignore[no-untyped-def]
    r = run["/start"]
    assert len(r["sent"]) == 1 and _texts(r)[0].startswith("<b>Торговый журнал</b>")
    assert r["loads"] == 0                                        # к бирже не ходил
    assert r["logs"] == [["Команда /start", "нет", None]]


def test_start_pos_opens_actions_with_note(run) -> None:  # type: ignore[no-untyped-def]
    r = run["/start pos-XRP-USDT-L"]
    assert r["loads"] >= 1
    assert any("Позиции XRP-USDT LONG на бирже уже нет." in t for t in _texts(r))
    assert ["Команда /start", "pos", None] in r["logs"]


def test_start_garbage_is_menu_without_exchange(run) -> None:  # type: ignore[no-untyped-def]
    r = run["/start мусор"]
    assert len(r["sent"]) == 1 and _texts(r)[0].startswith("<b>Торговый журнал</b>")
    assert r["loads"] == 0
    assert r["logs"] == [["Команда /start", "другой", None]]


def test_positions_screen_without_note_only_by_open_or_button(run) -> None:  # type: ignore[no-untyped-def]
    """Экран «💼 Позиции» без заметки — /open и кнопка меню; лог говорит, что вызвало."""
    for case, trigger in (("/open", "/open"), ("cb:menu:positions", None)):
        key = next(k for k in run if k == case or (case.startswith("cb:") and k.startswith("cb:")))
        r = run[key]
        texts = _texts(r)
        assert r["loads"] >= 1 and texts and "💼 Позиции" in texts[-1], key
        assert "уже нет" not in texts[-1], key
        logged = [x for x in r["logs"] if x[0] == "Экран позиций"]
        assert len(logged) == 1, key
        assert logged[0][2] == (trigger or key[3:]), key
