"""Мягкая остановка процесса бота по SIGTERM/SIGINT (план 27.10, docs/handoff.md).

Без неё aiogram на SIGTERM снимал только задачу поллинга, `finally` в run()
закрывал сессию Telegram, Redis и БД под обработчиками в полёте, а
`asyncio.run` отменял остаток — «Да» получало CancelledError посреди пути
(#40, 12.10), лок позиции снимался на уже закрытом Redis и висел до TTL.
`scheduler.shutdown(wait=False)` у asyncio-исполнителя APScheduler так же
отменял идущий цикл.

Здесь: учёт обработчиков и циклов в полёте, ожидание их общим сроком, явная
отмена не уложившихся — пока Redis и БД ещё открыты (их `finally` снимает
локи), и только потом закрытие ресурсов. Что не успело — доводит
восстановление при старте (B.9, открытия).
"""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import TelegramObject

from app.core.logging import get_logger

logger = get_logger(__name__)

# Общий срок ожидания обработчиков, циклов и быстрых повторов тревоги.
# docker-compose.yml: stop_grace_period бота (40 с) > срок + отмена + закрытие —
# иначе SIGKILL придёт раньше (tests/test_deploy_config.py).
BOT_SHUTDOWN_DRAIN_SECONDS = 25.0
# После отмены не уложившихся — сколько ждать их finally (снятие локов).
SHUTDOWN_CANCEL_WAIT_SECONDS = 3.0

UPDATE = "update"
CYCLE = "cycle"
ALARM_RETRY = "alarm_retry"


class StopSignal:
    """SIGTERM/SIGINT → флаг остановки. Свой обработчик вместо aiogram
    (`start_polling(handle_signals=False)`): сигнал во время восстановления на
    старте тоже ловится — следующий шаг не начинается."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.at: float | None = None
        self._installed: list[signal.Signals] = []

    def trigger(self, sig: signal.Signals | None = None) -> None:
        if self._event.is_set():
            return
        self.at = time.monotonic()
        self._event.set()
        logger.info("Получен сигнал остановки",
                    extra={"signal": sig.name if sig is not None else None})

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def install(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Windows (локальный запуск): обработчиков сигналов у цикла нет —
            # Ctrl+C завершает процесс, как до плана.
            with suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.trigger, sig)
                self._installed.append(sig)

    def uninstall(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in self._installed:
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        self._installed.clear()


class InFlight:
    """Задачи в полёте: обработчик апдейта (своя задача у каждого апдейта —
    `handle_as_tasks` aiogram) и фоновый цикл (своя задача у каждого запуска —
    asyncio-исполнитель APScheduler). Учёт — по текущей задаче, без приватных
    атрибутов aiogram."""

    def __init__(self) -> None:
        self._tasks: dict[asyncio.Task[Any], tuple[str, str]] = {}

    @contextmanager
    def track(self, kind: str, label: str) -> Iterator[None]:
        task = asyncio.current_task()
        if task is None:
            yield
            return
        self._tasks[task] = (kind, label)
        try:
            yield
        finally:
            self._tasks.pop(task, None)

    def pending(self) -> dict[asyncio.Task[Any], tuple[str, str]]:
        return {t: v for t, v in self._tasks.items() if not t.done()}


class InFlightMiddleware(BaseMiddleware):
    """Outer-middleware на update: обработчик в полёте до последней строки."""

    def __init__(self, inflight: InFlight) -> None:
        self._inflight = inflight

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        label = f"update_id={getattr(event, 'update_id', None)}"
        with self._inflight.track(UPDATE, label):
            return await handler(event, data)


async def poll_until_stopped(
    dp: Dispatcher, bot: Bot, stop: StopSignal, *, before_stop: Callable[[], None]
) -> None:
    """Поллинг до сигнала. Сессию Telegram aiogram не закрывает
    (`close_bot_session=False`): обработчики в полёте ещё отвечают в чат.
    before_stop — до стопа поллинга (новых циклов планировщика нет)."""
    polling = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False, close_bot_session=False)
    )
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()
    if not polling.done():
        before_stop()
        try:
            await dp.stop_polling()
        except RuntimeError:
            polling.cancel()       # поллинг ещё не начался — апдейтов не было
        with suppress(asyncio.CancelledError):
            await polling
        return
    if not polling.cancelled():
        polling.result()           # поллинг упал сам — исключение наверх, как раньше


async def drain(
    inflight: InFlight,
    extra: Mapping[asyncio.Task[Any], tuple[str, str]] | None = None,
    *,
    timeout: float = BOT_SHUTDOWN_DRAIN_SECONDS,
    cancel_wait: float = SHUTDOWN_CANCEL_WAIT_SECONDS,
) -> list[str]:
    """Ждёт задачи в полёте общим сроком; не уложившиеся — отмена и короткое
    ожидание их finally. Возвращает подписи отменённых."""
    tasks = {**inflight.pending(), **{t: v for t, v in (extra or {}).items() if not t.done()}}
    kinds = [kind for kind, _ in tasks.values()]
    logger.info(
        f"Остановка: обработчиков {kinds.count(UPDATE)}, циклов {kinds.count(CYCLE)}, "
        f"повторов тревоги {kinds.count(ALARM_RETRY)}"
    )
    if not tasks:
        return []
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    if not pending:
        return []
    labels = sorted(f"{tasks[t][0]} {tasks[t][1]}" for t in pending)
    logger.warning(
        f"Остановка: не уложились за {timeout:.0f} с — доведёт восстановление: "
        + ", ".join(labels)
    )
    for task in pending:
        task.cancel()
    await asyncio.wait(pending, timeout=cancel_wait)
    return labels


async def close_all(closers: Sequence[tuple[str, Callable[[], Awaitable[None]]]]) -> None:
    """Ресурсы — по порядку; сбой одного не оставляет открытыми остальные."""
    for name, close in closers:
        try:
            await close()
        except Exception:
            logger.exception("Остановка: ресурс не закрыт", extra={"resource": name})


def log_finished(started: float) -> None:
    logger.info(f"Остановка завершена за {time.monotonic() - started:.1f} с")
