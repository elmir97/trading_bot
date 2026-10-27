"""Мягкая остановка бота по SIGTERM (план 27.10, docs/handoff.md; app/core/shutdown.py).

Настоящий сигнал на Windows не послать — путь сигнала (loop.add_signal_handler)
проверяется на деплое: «Остановка завершена за X с» и ExitCode 0, не 137.
Здесь — всё, что за ним: учёт в полёте, порядок, срок, отмена, compose.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, Update, User
from fakeredis.aioredis import FakeRedis

from app import main as bot_main
from app.api.main import CONFIRM_DRAIN_SECONDS
from app.core.config import Settings
from app.core.locks import RedisLock
from app.core.shutdown import (
    ALARM_RETRY,
    BOT_SHUTDOWN_DRAIN_SECONDS,
    CYCLE,
    SHUTDOWN_CANCEL_WAIT_SECONDS,
    UPDATE,
    InFlight,
    InFlightMiddleware,
    StopSignal,
    drain,
    log_finished,
    poll_until_stopped,
)
from app.workers.base import job_wrapper
from app.workers.scheduler import BackgroundJobs
from tests.test_opening_faults import BASE

ROOT = Path(__file__).resolve().parents[1]


class FakeJobs:
    """BackgroundJobs для shutdown(): пауза, повторы тревоги, закрытие — в журнал."""

    def __init__(
        self, log: list[str], alarms: dict[asyncio.Task[Any], tuple[str, str]] | None = None
    ) -> None:
        self.log = log
        self._alarms = alarms or {}

    def pause(self) -> None:
        self.log.append("pause")

    def alarm_retries(self) -> dict[asyncio.Task[Any], tuple[str, str]]:
        return self._alarms

    async def shutdown(self) -> None:
        self.log.append("background_jobs")


def closers(log: list[str], jobs: FakeJobs, **extra: Any) -> list[tuple[str, Any]]:
    async def close(name: str) -> None:
        log.append(name)

    out: list[tuple[str, Any]] = [("background_jobs", jobs.shutdown)]
    for name in ("telegram", "redis", "db"):
        out.append((name, extra.get(name) or (lambda n=name: close(n))))
    return out


async def tracked(inflight: InFlight, kind: str, label: str, body: Any) -> None:
    with inflight.track(kind, label):
        await body()


def _update(update_id: int) -> Update:
    who = User(id=1, is_bot=False, first_name="t")
    message = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=1, type="private"),
                      from_user=who, text="x")
    return Update(update_id=update_id, message=message)


# --- учёт в полёте ----------------------------------------------------------------------


async def test_middleware_tracks_update_until_handler_returns() -> None:
    """Outer-middleware на update: обработчик в полёте до последней строки."""
    inflight = InFlight()
    dp = Dispatcher()
    dp.update.outer_middleware(InFlightMiddleware(inflight))
    router = Router()
    entered, release = asyncio.Event(), asyncio.Event()

    @router.message()
    async def handler(message: Message) -> None:
        entered.set()
        await release.wait()

    dp.include_router(router)
    bot = Bot(token="999:AAFakeTokenForShutdownTests")
    task = asyncio.create_task(dp.feed_update(bot, _update(77)))
    await asyncio.wait_for(entered.wait(), 2)
    assert list(inflight.pending().values()) == [(UPDATE, "update_id=77")]
    release.set()
    await task
    assert inflight.pending() == {}
    await bot.session.close()


async def test_job_wrapper_tracks_cycle() -> None:
    inflight = InFlight()
    seen: list[Any] = []

    async def cycle() -> None:
        seen.append(list(inflight.pending().values()))

    await job_wrapper("reconciler", cycle, inflight=inflight)()
    assert seen == [[(CYCLE, "reconciler")]] and inflight.pending() == {}


# --- порядок и срок ----------------------------------------------------------------------


async def test_handler_finishes_before_resources_close() -> None:
    """Начатый обработчик доживает; ресурсы закрываются после него, по порядку."""
    log: list[str] = []
    inflight = InFlight()
    jobs = FakeJobs(log)

    async def handler() -> None:
        await asyncio.sleep(0.2)
        log.append("handler_done")

    task = asyncio.create_task(tracked(inflight, UPDATE, "update_id=1", handler))
    await asyncio.sleep(0)
    await bot_main.shutdown(jobs, inflight, closers(log, jobs), timeout=5)  # type: ignore[arg-type]
    assert task.done() and not task.cancelled()
    assert log == ["pause", "handler_done", "background_jobs", "telegram", "redis", "db"]


async def test_hung_handler_cancelled_by_deadline_with_warning(caplog) -> None:  # type: ignore[no-untyped-def]
    log: list[str] = []
    inflight = InFlight()
    jobs = FakeJobs(log)

    async def hung() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            log.append("handler_cancelled")
            raise

    task = asyncio.create_task(tracked(inflight, UPDATE, "update_id=9", hung))
    await asyncio.sleep(0)
    with caplog.at_level(logging.INFO):
        await bot_main.shutdown(jobs, inflight, closers(log, jobs), timeout=0.2)  # type: ignore[arg-type]
    assert task.cancelled()
    assert log == ["pause", "handler_cancelled", "background_jobs", "telegram", "redis", "db"]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == (
        "Остановка: не уложились за 0 с — доведёт восстановление: update update_id=9")
    assert "Остановка: обработчиков 1, циклов 0, повторов тревоги 0" in caplog.messages


async def test_lock_released_by_cancelled_handler_while_redis_open() -> None:
    """Владелец 27.10: обработчик, отменённый по сроку, снимает лок позиции —
    Redis в этот момент ещё открыт (не висит до TTL)."""
    redis = FakeRedis(decode_responses=True)
    key = "exec:lock:1:pXRP-USDT:LONG"
    log: list[str] = []
    inflight = InFlight()
    jobs = FakeJobs(log)
    locked = asyncio.Event()

    async def holds_lock() -> None:
        async with RedisLock(redis, key, ttl_seconds=187):
            locked.set()
            await asyncio.sleep(3600)

    at_close: list[int] = []

    async def close_redis() -> None:
        at_close.append(await redis.exists(key))
        await redis.aclose()

    task = asyncio.create_task(tracked(inflight, UPDATE, "update_id=5", holds_lock))
    await asyncio.wait_for(locked.wait(), 2)
    assert await redis.exists(key) == 1
    await bot_main.shutdown(jobs, inflight, closers(log, jobs, redis=close_redis),  # type: ignore[arg-type]
                            timeout=0.1)
    assert task.cancelled()
    assert at_close == [0]          # лок снят до закрытия Redis


async def test_alarm_retries_waited_with_common_deadline() -> None:
    log: list[str] = []

    async def retry() -> None:
        await asyncio.sleep(0.2)
        log.append("retry_done")

    task = asyncio.create_task(retry())
    jobs = FakeJobs(log, {task: (ALARM_RETRY, "opening_id=3")})
    await bot_main.shutdown(jobs, InFlight(), closers(log, jobs), timeout=5)  # type: ignore[arg-type]
    assert log[:2] == ["pause", "retry_done"] and not task.cancelled()


async def test_drain_nothing_in_flight_returns_at_once() -> None:
    assert await asyncio.wait_for(drain(InFlight(), timeout=60), 1) == []


async def test_running_cycle_survives_and_no_new_cycle_after_stop() -> None:
    """Настоящий BackgroundJobs и APScheduler: идущий цикл доживает (shutdown
    asyncio-исполнителя его отменил бы), новый после остановки не стартует."""
    inflight = InFlight()
    jobs = BackgroundJobs(None, None, Settings(**BASE), None, None, inflight=inflight)  # type: ignore[arg-type]
    starts: list[float] = []
    finished: list[str] = []
    started = asyncio.Event()

    async def cycle() -> None:
        starts.append(asyncio.get_running_loop().time())
        started.set()
        await asyncio.sleep(0.3)
        finished.append("done")

    jobs._scheduler.add_job(job_wrapper("test_cycle", cycle, inflight=inflight), "interval",
                            seconds=0.05, id="test_cycle", coalesce=True, max_instances=1,
                            next_run_time=datetime.now(UTC))
    jobs._scheduler.start()
    await asyncio.wait_for(started.wait(), 2)
    await bot_main.shutdown(jobs, inflight, [("background_jobs", jobs.shutdown)], timeout=5)
    assert finished == ["done"] and len(starts) == 1
    await asyncio.sleep(0.2)
    assert len(starts) == 1 and not jobs._scheduler.running


# --- старт и поллинг ---------------------------------------------------------------------


async def test_signal_during_startup_recovery_skips_next_step() -> None:
    stop = StopSignal()
    calls: list[str] = []

    async def openings_run() -> None:
        calls.append("openings")
        stop.trigger()

    async def recover_actions() -> int:
        calls.append("actions")
        return 0

    jobs = SimpleNamespace(openings=SimpleNamespace(run=openings_run),
                           recover_actions=recover_actions)
    settings = SimpleNamespace(background_jobs_enabled=True)
    await bot_main.recover_on_start(jobs, settings, stop)  # type: ignore[arg-type]
    assert calls == ["openings"]


async def test_startup_recovery_both_steps_without_signal() -> None:
    calls: list[str] = []

    async def step(name: str) -> int:
        calls.append(name)
        return 0

    jobs = SimpleNamespace(openings=SimpleNamespace(run=lambda: step("openings")),
                           recover_actions=lambda: step("actions"))
    await bot_main.recover_on_start(jobs, SimpleNamespace(background_jobs_enabled=True),  # type: ignore[arg-type]
                                    StopSignal())
    assert calls == ["openings", "actions"]


class FakeDp:
    def __init__(self, log: list[str], fail: Exception | None = None) -> None:
        self.log, self.fail = log, fail
        self.kwargs: dict[str, Any] = {}
        self._stop = asyncio.Event()

    async def start_polling(self, bot: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        if self.fail is not None:
            raise self.fail
        await self._stop.wait()
        self.log.append("polling_stopped")

    async def stop_polling(self) -> None:
        self.log.append("stop_polling")
        self._stop.set()


async def test_poll_own_signals_bot_session_left_open() -> None:
    """aiogram не ставит свои обработчики сигналов и не закрывает сессию Telegram
    (обработчики в полёте ещё отвечают в чат); планировщик — до стопа поллинга."""
    log: list[str] = []
    dp, stop = FakeDp(log), StopSignal()
    task = asyncio.create_task(poll_until_stopped(dp, object(), stop,  # type: ignore[arg-type]
                                                  before_stop=lambda: log.append("pause")))
    await asyncio.sleep(0.05)
    stop.trigger()
    await asyncio.wait_for(task, 2)
    assert dp.kwargs == {"handle_signals": False, "close_bot_session": False}
    assert log == ["pause", "stop_polling", "polling_stopped"]


async def test_poll_failure_propagates() -> None:
    dp = FakeDp([], fail=RuntimeError("сеть"))
    with pytest.raises(RuntimeError, match="сеть"):
        await poll_until_stopped(dp, object(), StopSignal(), before_stop=lambda: None)  # type: ignore[arg-type]


def test_stop_signal_trigger_once_and_install_safe() -> None:
    async def body() -> None:
        stop = StopSignal()
        stop.install()          # Windows: обработчиков нет — без исключения
        try:
            assert not stop.is_set() and stop.at is None
            stop.trigger()
            first = stop.at
            stop.trigger()
            assert stop.is_set() and stop.at == first
        finally:
            stop.uninstall()

    asyncio.run(body())


def test_log_finished_line(caplog) -> None:  # type: ignore[no-untyped-def]
    import time

    with caplog.at_level(logging.INFO):
        log_finished(time.monotonic() - 2.04)
    assert re.fullmatch(r"Остановка завершена за 2\.\d с", caplog.messages[-1])


# --- проводка run() ----------------------------------------------------------------------


async def test_run_wires_inflight_and_closes_after_handler(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """run(): учёт обработчиков на диспетчере и циклов в BackgroundJobs — один и
    тот же; планировщик — до стопа поллинга; обработчик в полёте доживает до
    закрытия ресурсов; ресурсы — в порядке плана."""
    log: list[str] = []
    seen: dict[str, Any] = {}

    class FakeSession:
        async def close(self) -> None:
            log.append("telegram")

    class FakeBot:
        id = 1

        def __init__(self, **kwargs: Any) -> None:
            self.session = FakeSession()

        async def delete_webhook(self, **kwargs: Any) -> None:
            log.append("delete_webhook")

    class FakeRedisClient:
        async def aclose(self) -> None:
            log.append("redis")

    class FakeDb:
        def __init__(self, settings: Any) -> None:
            pass

        async def healthcheck(self) -> None:
            pass

        async def dispose(self) -> None:
            log.append("db")

    class Jobs(FakeJobs):
        def __init__(self, *args: Any, inflight: InFlight | None = None, **kwargs: Any
                     ) -> None:
            super().__init__(log)
            seen["jobs_inflight"] = inflight

            async def run() -> None:
                log.append("recover_openings")

            self.openings = SimpleNamespace(run=run, retry_alarm=lambda opening_id: None)

        async def recover_actions(self) -> int:
            log.append("recover_actions")
            return 0

        def start(self) -> None:
            log.append("start")

    async def fake_poll(dp: Dispatcher, bot: Any, stop: StopSignal, *, before_stop: Any) -> None:
        [middleware] = [m for m in dp.update.outer_middleware if isinstance(m, InFlightMiddleware)]
        seen["dp_inflight"] = middleware._inflight

        async def handler(event: Any, data: Any) -> None:
            await asyncio.sleep(0.2)
            log.append("handler_done")

        seen["handler"] = asyncio.create_task(middleware(handler, _update(3), {}))
        await asyncio.sleep(0)
        log.append("polling")
        before_stop()
        stop.trigger()

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(bot_main, "get_settings", lambda: Settings(
        **BASE, ai_enabled=False, background_jobs_enabled=True))
    monkeypatch.setattr(bot_main, "setup_logging", lambda **kwargs: None)
    monkeypatch.setattr(bot_main, "Database", FakeDb)
    monkeypatch.setattr(bot_main, "bootstrap_reference_data", noop)
    monkeypatch.setattr(bot_main, "Bot", FakeBot)
    monkeypatch.setattr(bot_main.Redis, "from_url", lambda *a, **k: FakeRedisClient())
    monkeypatch.setattr(bot_main, "build_dispatcher", lambda *a: Dispatcher())
    monkeypatch.setattr(bot_main, "BackgroundJobs", Jobs)
    monkeypatch.setattr(bot_main, "setup_bot_ui", noop)
    monkeypatch.setattr(bot_main, "poll_until_stopped", fake_poll)

    with caplog.at_level(logging.INFO):
        await bot_main.run()
    assert re.fullmatch(r"Остановка завершена за \d+\.\d с", caplog.messages[-1])
    assert seen["dp_inflight"] is seen["jobs_inflight"]
    assert seen["handler"].done() and not seen["handler"].cancelled()
    assert log == ["recover_openings", "recover_actions", "start", "delete_webhook", "polling",
                   "pause", "pause", "handler_done", "background_jobs", "telegram", "redis", "db"]


# --- compose -----------------------------------------------------------------------------


def _seconds(value: str) -> float:
    m = re.fullmatch(r"(\d+)s", value)
    assert m, value
    return float(m.group(1))


def test_stop_grace_covers_drain() -> None:
    """grace > дренаж + запас у обоих: иначе SIGKILL (137) посреди «Да»."""
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    margin = 5
    bot_grace = _seconds(services["bot"]["stop_grace_period"])
    assert bot_grace == 40
    assert bot_grace >= BOT_SHUTDOWN_DRAIN_SECONDS + SHUTDOWN_CANCEL_WAIT_SECONDS + margin
    api = services["api"]
    cmd = api["command"]
    graceful = float(cmd[cmd.index("--timeout-graceful-shutdown") + 1])
    api_grace = _seconds(api["stop_grace_period"])
    assert (graceful, api_grace) == (20, 60)
    assert api_grace >= graceful + CONFIRM_DRAIN_SECONDS + margin
