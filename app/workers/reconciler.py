"""Сверка журнала с биржей — фоновая задача (шаг 15.6, раздел 10 ТЗ).

Решения — app/execution/reconciler.py (без I/O); здесь запросы к бирже,
запись в журнал, события reconciliation_events и уведомления.

Цикл пользователя: позиции (один запрос) → по каждой открытой сделке бота:
позиция уменьшилась/исчезла → история ордеров → выходы фактом биржи или
расхождение; раз в reconciler_stop_check_every циклов — openOrders, есть ли
стоп → позиции без сделки в журнале → входы UNKNOWN/PENDING/SUBMITTED
старше окна.

Гонки с путём «Да»: пока в Redis жив хоть один exec:lock:* — цикл
пропускается целиком (вход в полёте, и лимиты BingX секундные — не
отнимаем их у «Да»); символы с незакрытым входом моложе окна не считаются
«позицией без сделки». Двойная запись выхода невозможна: строка сделки под
FOR UPDATE, статус перепроверяется, external_fill_id = orderId биржи под
uq_fill_external_id. Ордеров reconciler не отправляет никогда.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.input_prompt import InputGate
from app.core.logging import get_logger
from app.core.numfmt import fmt_amount, fmt_money, fmt_price, fmt_qty
from app.core.security import SecretCipher
from app.core.timefmt import closed_at_line
from app.database.models.execution_order import ExecutionOrder
from app.database.models.reconciliation_event import ReconciliationEvent
from app.database.models.trade import Trade
from app.database.models.user import User, UserSettings
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.database.repositories.reconciliation_event import ReconciliationEventRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    ExchangeClient,
    ExchangeError,
    ExchangeResponseError,
    OrderFill,
    Position,
)
from app.exchanges.bingx import _QUOTE_ASSET_BY_MODE
from app.execution.position_view import (
    apply_exchange_levels,
    protective_orders,
    tracks_exchange,
)
from app.execution.reconciler import (
    ORDER_NOT_EXIST_CODE,
    UNRESOLVED_ENTRY_WINDOW,
    BotTradeSnapshot,
    Discrepancy,
    ExitFill,
    UnresolvedEntry,
    decide_trade,
    entry_is_due,
    exchange_position,
    needs_history,
    orphan_positions,
    pnl_mismatch,
    resolve_entry,
    stop_missing,
)
from app.execution.redelivery import (
    REDELIVERY_FAST,
    REDELIVERY_SLOW_INTERVAL,
    late_notice,
    redelivery_due,
)
from app.services.exchange_factory import ExchangeFactory
from app.trading.enums import (
    FillSide,
    OrderRole,
    OrderStatus,
    ReconciliationKind,
    TradeSource,
    TradeStatus,
)
from app.trading.exit_reasons import EXIT_MANUAL_STOP, EXIT_MANUAL_TAKE
from app.trading.journal import JournalError, TradeJournal
from app.trading.risk import tz_offset_for
from app.workers.base import fmt_decimal
from app.workers.notifier import Delivery, deliver_event

logger = get_logger(__name__)

ZERO = Decimal(0)
LOCK_PATTERN = "exec:lock:*"
# 03.10: некритичные расхождения, которые ждут, пока пользователь вводит число.
JOURNAL_OPEN_PREFIX = "journal_open:"


def deferrable(event: ReconciliationEvent) -> bool:
    return event.kind is ReconciliationKind.ORPHAN_POSITION or event.dedup_key.startswith(
        JOURNAL_OPEN_PREFIX
    )
HISTORY_LIMIT = timedelta(days=7) - timedelta(minutes=1)
# История ордеров сделки не разбирается дольше — одно событие владельцу
# (решение 29.09); до того — WARNING и ошибка в пульсе каждый цикл.
HISTORY_UNPARSED_ALERT_AFTER = timedelta(minutes=30)

_CLOSE_TEXT = {
    ReconciliationKind.CLOSED_STOP_LOSS: "🛑 {symbol} {side} закрыта по стопу на бирже",
    ReconciliationKind.CLOSED_TAKE_PROFIT: "🎯 {symbol} {side} закрыта по тейку на бирже",
    # 29.09: ℹ️, а не ⚠️ — закрытие вне бота владельцем штатно, тревоги нет.
    ReconciliationKind.CLOSED_OUTSIDE_BOT: "ℹ️ {symbol} {side} закрыта на бирже вне бота",
    ReconciliationKind.PARTIAL_CLOSE: "⚠️ {symbol} {side} частично закрыта на бирже",
}

# Причина закрытия вне бота в уведомлении — коротко: «вне бота» уже в
# заголовке. Trade.exit_reason в журнале — полный текст (app.trading.
# exit_reasons), по нему отчёт считает закрытия. Ручной маркет/лимит
# (EXIT_OUTSIDE_BOT) — без строки причины.
_NOTIFY_REASON = {
    EXIT_MANUAL_STOP: "Стоп, изменённый вручную",
    EXIT_MANUAL_TAKE: "Тейк, изменённый вручную",
}


def _open_quantity(trade: Trade) -> Decimal:
    entered = sum((f.quantity for f in trade.fills if f.fill_side is FillSide.ENTRY), ZERO)
    exited = sum((f.quantity for f in trade.fills if f.fill_side is FillSide.EXIT), ZERO)
    return entered - exited


@dataclass(frozen=True, slots=True)
class ReconcilerWindow:
    """Пульс reconciler за окно сводки исполнения (28.09). since_start —
    процесс стартовал внутри окна: счёт идёт «с HH:MM», а не за 24 ч."""

    started_at: datetime
    since_start: bool
    cycles: int
    errors: int
    last_cycle_at: datetime | None


class ReconcilerPulse:
    """Счётчики reconciler в памяти (решение 28.09): рестарт их сбрасывает,
    сводка пишет «с HH:MM». Два среза: окно для сводки (время циклов и
    ошибки за последние сутки) и счётчики с прошлой INFO-строки пульса."""

    def __init__(self, started_at: datetime) -> None:
        self.started_at = started_at
        self._cycles: deque[tuple[datetime, int]] = deque()
        self._reset_log(started_at)

    def _reset_log(self, now: datetime) -> None:
        self.log_since = now
        self.log_runs = 0
        self.log_cycles = 0
        self.log_skipped = 0
        self.log_errors = 0
        self.log_events = 0
        self.log_redelivered = 0

    def record_skip(self) -> None:
        self.log_runs += 1
        self.log_skipped += 1

    def record_cycle(self, now: datetime, *, errors: int) -> None:
        self._cycles.append((now, errors))
        self.log_runs += 1
        self.log_cycles += 1
        self.log_errors += errors

    def window(self, now: datetime, span: timedelta = timedelta(hours=24)) -> ReconcilerWindow:
        start = now - span
        while self._cycles and self._cycles[0][0] < start:
            self._cycles.popleft()
        return ReconcilerWindow(
            started_at=self.started_at,
            since_start=self.started_at > start,
            cycles=len(self._cycles),
            errors=sum(errors for _, errors in self._cycles),
            last_cycle_at=self._cycles[-1][0] if self._cycles else None,
        )

    def log_if_due(self, now: datetime, every: int) -> None:
        """INFO раз в every запусков (циклы + пропуски по локу)."""
        if self.log_runs < max(every, 1):
            return
        minutes = max(round((now - self.log_since).total_seconds() / 60), 1)
        last = self._cycles[-1][0] if self._cycles else None
        logger.info(
            f"Пульс reconciler: циклов {self.log_cycles} за {minutes} мин, "
            f"пропущено по локу {self.log_skipped}, ошибок {self.log_errors}, "
            f"событий {self.log_events}, переотправлено {self.log_redelivered}, "
            f"последний {f'{last:%H:%M:%S} UTC' if last else '—'}"
        )
        self._reset_log(now)


class Reconciler:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        settings: Settings,
        cipher: SecretCipher,
        redis: Any = None,
        *,
        input_gate: InputGate | None = None,
    ) -> None:
        self._bot = bot
        self._input_gate = input_gate
        self._db = db
        self._settings = settings
        self._cipher = cipher
        self._redis = redis
        self._cycle = 0
        self._cycle_errors = 0
        # 28.09: пульс — INFO раз в reconciler_pulse_every запусков и строка
        # «Сверка:» в сводке исполнения (DailyJobs читает pulse.window()).
        self.pulse = ReconcilerPulse(datetime.now(UTC))
        # trade_id → (user_id, с какого момента история ордеров сделки не
        # разбирается подряд). В памяти, как пульс: рестарт начинает отсчёт
        # 30 минут заново.
        self._history_unparsed_since: dict[int, tuple[int, datetime]] = {}

    async def run(self) -> None:
        self._cycle += 1
        if await self._confirm_in_flight():
            logger.info("Сверка пропущена: идёт действие с позицией (exec:lock)")
            self.pulse.record_skip()
            self.pulse.log_if_due(datetime.now(UTC), self._settings.reconciler_pulse_every)
            return
        self._cycle_errors = 0
        try:
            await self._run_cycle()
        except Exception:
            self._cycle_errors += 1
            raise
        finally:
            now = datetime.now(UTC)
            self.pulse.record_cycle(now, errors=self._cycle_errors)
            self.pulse.log_if_due(now, self._settings.reconciler_pulse_every)

    async def _run_cycle(self) -> None:
        # openOrders — на первом цикле после старта и дальше раз в N циклов.
        every = max(self._settings.reconciler_stop_check_every, 1)
        check_stops = (self._cycle - 1) % every == 0
        async with self._db.session() as session:
            user_ids = await ExecutionOrderRepository(session).users_with_real_entries()
        # 28.09: переотправка — до запросов к бирже и в своей транзакции:
        # недоступность BingX не должна задерживать уведомления. События
        # бывают только у пользователей с реальными входами.
        try:
            await self._redeliver(user_ids)
        except Exception:
            self._cycle_errors += 1
            logger.exception("Переотправка уведомлений сверки упала")
        for user_id in user_ids:
            try:
                async with self._db.session() as session:
                    await self._reconcile_user(session, user_id, check_stops=check_stops)
            except ExchangeError:
                self._cycle_errors += 1
                logger.warning(
                    "Сверка пользователя не удалась — биржа", extra={"user_id": user_id},
                    exc_info=True,
                )
            except Exception:
                self._cycle_errors += 1
                logger.exception("Сверка пользователя упала", extra={"user_id": user_id})

    async def _redeliver(self, user_ids: list[int]) -> None:
        """Уведомления «хотя бы один раз» (28.09): события с notified_at IS
        NULL — по расписанию app/execution/redelivery.py. Старше
        reconciler_notify_max_age_hours — отказ с WARNING; первый цикл после
        быстрого окна — один ERROR."""
        max_age = timedelta(hours=self._settings.reconciler_notify_max_age_hours)
        async with self._db.session() as session:
            pending = await ReconciliationEventRepository(session).list_undelivered(user_ids)
            now = datetime.now(UTC)
            for event, user in pending:
                decision = redelivery_due(
                    created_at=event.created_at,
                    last_attempt_at=event.last_attempt_at,
                    now=now,
                    fast=REDELIVERY_FAST,
                    slow_interval=REDELIVERY_SLOW_INTERVAL,
                    max_age=max_age,
                )
                extra = {
                    "event_id": event.id, "kind": event.kind.value, "symbol": event.symbol,
                    "attempts": event.attempts,
                }
                if decision.expired:
                    event.gave_up_at = now
                    logger.warning(
                        "Уведомление сверки не доставлено за "
                        f"{self._settings.reconciler_notify_max_age_hours} ч — отказ",
                        extra=extra,
                    )
                    continue
                if not decision.due:
                    continue
                if decision.entering_slow:
                    logger.error(
                        "Уведомление сверки не доставлено за "
                        f"{int(REDELIVERY_FAST.total_seconds() // 60)} мин — дальше раз в "
                        f"{int(REDELIVERY_SLOW_INTERVAL.total_seconds() // 60)} мин",
                        extra=extra,
                    )
                text = _redelivery_text(event, user, now)
                if await self._attempt(event, user.telegram_id, now, text) is Delivery.DELIVERED:
                    self.pulse.log_redelivered += 1

    async def _attempt(
        self, event: ReconciliationEvent, telegram_id: int, now: datetime, text: str
    ) -> Delivery | None:
        """None — отложено (03.10): пользователь вводит число, событие
        некритичное. Попытка не засчитывается, событие ждёт переотправки."""
        if (
            deferrable(event) and self._input_gate is not None
            and await self._input_gate.entering(telegram_id, now)
        ):
            logger.info(
                "Уведомление сверки отложено: идёт ввод числа",
                extra={"event_id": event.id, "kind": event.kind.value},
            )
            return None
        return await deliver_event(self._bot, event, telegram_id, now, text)

    async def _confirm_in_flight(self) -> bool:
        if self._redis is None:
            return False
        async for _key in self._redis.scan_iter(match=LOCK_PATTERN, count=100):
            return True
        return False

    # --- Пользователь -----------------------------------------------------

    async def _reconcile_user(
        self, session: AsyncSession, user_id: int, *, check_stops: bool
    ) -> None:
        user = await UserRepository(session).get_by_id(user_id)
        if user is None:
            return
        now = datetime.now(UTC)
        mode = self._settings.bingx_allowed_exchange_mode
        asset = _QUOTE_ASSET_BY_MODE[mode]
        client = await ExchangeFactory(self._settings, self._cipher).for_user(
            session, user_id, mode=mode
        )
        # Явным запросом: get_by_id не грузит settings, а ленивая загрузка в
        # async-сессии падает (MissingGreenlet).
        timezone = await session.scalar(
            select(UserSettings.timezone).where(UserSettings.user_id == user_id)
        )
        ctx = _UserCtx(
            session, user.telegram_id, user_id, asset, now, tz_offset_for(timezone)
        )
        try:
            positions = await client.get_positions()
            trades_repo = TradeRepository(session)
            orders_repo = ExecutionOrderRepository(session)
            open_trades = await trades_repo.list_open_for_reconcile(user_id)
            unresolved = await orders_repo.list_unresolved_entries(user_id)
            journal_open_keys: set[str] = set()

            for trade in open_trades:
                if trade.source is TradeSource.SIGNAL_EXECUTION:
                    if not trade.fill_confirmed:
                        continue
                    await self._reconcile_trade(ctx, client, trade, positions, check_stops)
                elif tracks_exchange(
                    trade, _open_quantity(trade),
                    live := exchange_position(positions, trade.symbol, trade.side),
                ):
                    # Связь по символу и стороне — пока позиция жива; positionId
                    # держит её и после закрытия позиции (02.10, #12: импорт без
                    # positionId, стоп сработал — сделка осталась OPEN).
                    if (
                        trade.external_position_id is None
                        and live is not None and live.position_id
                    ):
                        trade.external_position_id = live.position_id
                        # flush сразу: list_open_for_reconcile ниже идёт с
                        # populate_existing и затёр бы несохранённое значение.
                        await session.flush()
                        logger.info(
                            "Связь сделки с позицией биржи записана",
                            extra={"trade_id": trade.id, "position_id": live.position_id},
                        )
                    # Этап 3: импортированная/ручная сделка, связанная с
                    # позицией на бирже, — закрытия фактом биржи. Тревоги
                    # «позиция без стопа» по ним нет: ручную торговлю без
                    # стопа бот не запрещает.
                    await self._reconcile_trade(
                        ctx, client, trade, positions, check_stops=False, bot=False
                    )
                elif trade.source is TradeSource.IMPORTED and _open_quantity(trade) > 0:
                    # 03.10: импортированная сделка без positionId, позиции на
                    # бирже нет — связь не увидена ни разу (позиция закрылась до
                    # первого цикла). Журнал не правим (чужие выходы приписать
                    # нельзя) — одно уведомление на сделку.
                    key = f"{JOURNAL_OPEN_PREFIX}{trade.id}"
                    journal_open_keys.add(key)
                    await self._discrepancy(ctx, Discrepancy(
                        kind=ReconciliationKind.AMBIGUOUS, dedup_key=key, symbol=trade.symbol,
                        detail=(
                            f"сделка #{trade.id} {trade.side.value} открыта в журнале, позиции "
                            "на бирже нет — закрой её или проверь"
                        ),
                        trade_id=trade.id,
                    ))
            await self._resolve_missing(
                ctx, ReconciliationKind.AMBIGUOUS, journal_open_keys, prefix=JOURNAL_OPEN_PREFIX
            )
            # Сделка закрыта или ушла из сверки — её отсчёт сбоя не нужен.
            open_ids = {t.id for t in open_trades}
            for trade_id, (owner, _since) in list(self._history_unparsed_since.items()):
                if owner == user_id and trade_id not in open_ids:
                    del self._history_unparsed_since[trade_id]

            in_flight = {
                e.symbol for e in unresolved if now - e.created_at < UNRESOLVED_ENTRY_WINDOW
            }
            journal_open = {
                (t.symbol, t.side)
                for t in await trades_repo.list_open_for_reconcile(user_id)
            }
            orphans = orphan_positions(positions, journal_open, in_flight)
            for found in orphans:
                await self._discrepancy(ctx, found)
            await self._resolve_missing(
                ctx, ReconciliationKind.ORPHAN_POSITION, {d.dedup_key for d in orphans}
            )

            for entry in unresolved:
                await self._resolve_unresolved_entry(ctx, client, entry, positions)
            if check_stops:
                # Последним перед commit: list_open_for_reconcile выше идёт с
                # populate_existing и перечитал бы несохранённые уровни.
                await self._sync_levels(
                    ctx, client, await trades_repo.list_open_for_reconcile(user_id), positions
                )
            await session.commit()
        finally:
            await client.close()

    # --- Сделка -----------------------------------------------------------

    async def _reconcile_trade(
        self,
        ctx: _UserCtx,
        client: ExchangeClient,
        trade: Trade,
        positions: list[Position],
        check_stops: bool,
        *,
        bot: bool = True,
    ) -> None:
        conditionals = (
            await ExecutionOrderRepository(ctx.session).conditionals_for_notification(
                ctx.user_id, trade.notification_id
            )
            if bot and trade.notification_id is not None
            else []
        )
        stop_row = next((c for c in conditionals if c.role is OrderRole.STOP_LOSS), None)
        take_row = next((c for c in conditionals if c.role is OrderRole.TAKE_PROFIT), None)
        snapshot = BotTradeSnapshot(
            trade_id=trade.id,
            symbol=trade.symbol,
            side=trade.side,
            open_quantity=_open_quantity(trade),
            opened_at=trade.opened_at,
            stop_order_id=stop_row.exchange_order_id if stop_row else None,
            take_order_id=take_row.exchange_order_id if take_row else None,
            recorded_order_ids=frozenset(
                f.external_fill_id for f in trade.fills if f.external_fill_id
            ),
            exits_after=(
                None if bot or not trade.fills else max(f.executed_at for f in trade.fills)
            ),
        )
        position = exchange_position(positions, trade.symbol, trade.side)

        if not needs_history(snapshot, position):
            self._history_unparsed_since.pop(trade.id, None)
            await self._resolve_trade_discrepancies(ctx, trade.id)
            if check_stops:
                open_orders = await client.get_open_orders(trade.symbol)
                missing = stop_missing(snapshot, position, open_orders)
                if missing is not None:
                    await self._discrepancy(ctx, missing, alarm=True)
                else:
                    await self._resolve_missing(
                        ctx, ReconciliationKind.STOP_MISSING, set(),
                        prefix=f"stop_missing:{trade.id}:",
                    )
            return

        start = max(trade.opened_at - timedelta(minutes=1), ctx.now - HISTORY_LIMIT)
        try:
            orders = await client.get_all_orders(trade.symbol, start, ctx.now)
        except ExchangeResponseError as exc:
            await self._history_unparsed(ctx, trade, exc)
            return
        if self._history_unparsed_since.pop(trade.id, None) is not None:
            await self._resolve_missing(
                ctx, ReconciliationKind.AMBIGUOUS, set(), prefix=f"history:{trade.id}:"
            )
        decision = decide_trade(snapshot, position, orders)
        if decision.discrepancy is not None:
            await self._discrepancy(ctx, decision.discrepancy)
            return
        if not decision.exits:
            return
        await self._record_exits(
            ctx, trade.id, decision.exits, decision.closes_fully, stop_row, take_row
        )

    async def _sync_levels(
        self,
        ctx: _UserCtx,
        client: ExchangeClient,
        open_trades: list[Trade],
        positions: list[Position],
    ) -> None:
        """Этап 3: стоп и тейк сделок журнала следуют за ордерами на бирже
        (перенос в безубыток, ручная правка) — app/execution/position_view.py.
        Один openOrders на все символы, только если есть сделки с позицией.
        Сбой биржи — пропуск до следующей проверки, цикл не роняет."""
        tracked = [
            (trade, position)
            for trade in open_trades
            if (position := exchange_position(positions, trade.symbol, trade.side)) is not None
        ]
        if not tracked:
            return
        try:
            orders = await client.get_open_orders()
        except ExchangeError:
            logger.warning("openOrders для уровней не получены", extra={"user_id": ctx.user_id})
            return
        for trade, position in tracked:
            stops, takes = protective_orders(position, orders)
            changes = apply_exchange_levels(trade, stops, takes)
            if changes:
                logger.info(
                    "Уровни сделки обновлены по бирже",
                    extra={"trade_id": trade.id, "changes": "; ".join(changes)},
                )

    async def _history_unparsed(
        self, ctx: _UserCtx, trade: Trade, exc: ExchangeResponseError
    ) -> None:
        """История ордеров сделки пришла, но не разбирается (29.09: пустая
        цифра исполненного выхода — ошибка, не 0). Журнал не трогаем; каждый
        цикл — WARNING и ошибка в пульсе, остальные сделки сверяются дальше.
        Сбой подряд дольше HISTORY_UNPARSED_ALERT_AFTER — одно событие
        владельцу (дедуп по ключу), разрешается, когда история разобралась."""
        self._cycle_errors += 1
        _owner, since = self._history_unparsed_since.setdefault(
            trade.id, (ctx.user_id, ctx.now)
        )
        logger.warning(
            "Сверка сделки: история ордеров не разобрана — журнал не изменён",
            extra={
                "trade_id": trade.id, "symbol": trade.symbol, "error": str(exc),
                "since": since.isoformat(),
            },
        )
        if ctx.now - since <= HISTORY_UNPARSED_ALERT_AFTER:
            return
        minutes = int((ctx.now - since).total_seconds() // 60)
        await self._discrepancy(
            ctx,
            Discrepancy(
                ReconciliationKind.AMBIGUOUS, f"history:{trade.id}:unparsed", trade.symbol,
                f"история ордеров сделки #{trade.id} не разбирается {minutes} мин ({exc}) — "
                "закрытие с биржи в журнал не записано",
                trade_id=trade.id,
            ),
        )

    async def _record_exits(
        self,
        ctx: _UserCtx,
        trade_id: int,
        exits: list[ExitFill],
        closes_fully: bool,
        stop_row: ExecutionOrder | None,
        take_row: ExecutionOrder | None,
    ) -> None:
        trades = TradeRepository(ctx.session)
        trade = await trades.lock_for_reconcile(trade_id)
        if trade is None or trade.status is not TradeStatus.OPEN:
            return  # закрыли руками или параллельной сверкой — факт уже в журнале
        journal = TradeJournal(trades)
        for exit_fill in exits:
            try:
                async with ctx.session.begin_nested():
                    await journal.close_trade(
                        trade,
                        exit_price=exit_fill.price,
                        quantity=exit_fill.quantity,
                        fee=exit_fill.fee,
                        exit_reason=exit_fill.reason,
                        closed_at=exit_fill.executed_at,
                        external_fill_id=exit_fill.order_id,
                        exchange_realized_pnl=exit_fill.realized_pnl,
                    )
            except (IntegrityError, JournalError):
                logger.warning(
                    "Выход уже записан или не записывается — пропускаю",
                    extra={"trade_id": trade_id, "order_id": exit_fill.order_id},
                    exc_info=True,
                )
                return
            await self._fact(
                ctx,
                ReconciliationEvent(
                    user_id=ctx.user_id, trade_id=trade.id, symbol=trade.symbol,
                    kind=exit_fill.kind, dedup_key=f"close:{trade.id}:{exit_fill.order_id}",
                    detail=f"{exit_fill.reason}: {exit_fill.quantity} по {exit_fill.price}",
                ),
                self._close_text(trade, exit_fill, ctx.asset, ctx.tz_offset_hours),
            )
        if closes_fully:
            # Условник, который сработал, — FILLED; второй биржа сняла сама
            # при закрытии позиции (живьём 27.09: TP → CANCELLED) — CANCELED.
            fired_kinds = {e.kind for e in exits}
            fired_role = {
                OrderRole.STOP_LOSS: ReconciliationKind.CLOSED_STOP_LOSS in fired_kinds,
                OrderRole.TAKE_PROFIT: ReconciliationKind.CLOSED_TAKE_PROFIT in fired_kinds,
            }
            for row in (stop_row, take_row):
                if row is None or row.status in (OrderStatus.FILLED, OrderStatus.CANCELED):
                    continue
                row.status = OrderStatus.FILLED if fired_role[row.role] else OrderStatus.CANCELED
            triggered = {e.order_id for e in exits}
            logger.info(
                "Сделка закрыта сверкой с биржей",
                extra={"trade_id": trade.id, "orders": sorted(triggered)},
            )
            await self._resolve_trade_discrepancies(ctx, trade.id)
            await self._resolve_missing(
                ctx, ReconciliationKind.STOP_MISSING, set(), prefix=f"stop_missing:{trade.id}:"
            )
            # closed_at, не status: mypy сузил status до OPEN проверкой выше,
            # а close_trade меняет его на месте.
            if trade.closed_at is not None:
                await self._check_pnl(ctx, trade)

    async def _check_pnl(self, ctx: _UserCtx, trade: Trade) -> None:
        """28.09: PnL журнала против биржи при полном закрытии (PNL_MISMATCH).
        Не сверяем, если у части выходов нет profit биржи (ручные, записанные
        до 28.09) или 1R неизвестен — это не расхождение, а нехватка данных."""
        exits = [f for f in trade.fills if f.fill_side is FillSide.EXIT]
        realized = [f.exchange_realized_pnl for f in exits]
        entry = await ExecutionOrderRepository(ctx.session).entry_for_trade(
            ctx.user_id, trade.id
        )
        risk_amount = entry.risk_amount if entry is not None else None
        risk_stop = trade.risk_stop
        if (risk_amount is None or risk_amount <= 0) and trade.entry_price and risk_stop:
            entry_qty = sum(
                (f.quantity for f in trade.fills if f.fill_side is FillSide.ENTRY), Decimal(0)
            )
            risk_amount = abs(trade.entry_price - risk_stop) * entry_qty
        if (
            trade.pnl is None
            or not exits
            or any(value is None for value in realized)
            or risk_amount is None
            or risk_amount <= 0
        ):
            logger.info(
                "PnL с биржей не сверен: не хватает данных",
                extra={"trade_id": trade.id, "exits": len(exits)},
            )
            return
        found = pnl_mismatch(
            trade_id=trade.id,
            symbol=trade.symbol,
            journal_pnl=trade.pnl,
            fees=trade.fees,
            exits_realized_pnl=[value for value in realized if value is not None],
            risk_amount=risk_amount,
        )
        if found is not None:
            # Сделка закрыта — ждать исчезновения расхождения нечего: событие
            # сразу разрешено, уведомление одно.
            await self._discrepancy(ctx, found, resolve_now=True)

    @staticmethod
    def _close_text(
        trade: Trade, exit_fill: ExitFill, asset: str, tz_offset_hours: int = 5
    ) -> str:
        """Уведомление о закрытии — одно для всех видов (стоп/тейк бота,
        вне бота, частичное): время исполнения ордера в поясе пользователя
        (app.core.timefmt), суммы — форматтерами app.core.numfmt; комиссии —
        fmt_amount (без «+»), PnL — fmt_money (знак несёт смысл)."""
        lines = [_CLOSE_TEXT[exit_fill.kind].format(symbol=trade.symbol, side=trade.side.value)]
        if exit_fill.kind is ReconciliationKind.CLOSED_OUTSIDE_BOT:
            reason = _NOTIFY_REASON.get(exit_fill.reason)
            if reason is not None:
                lines.append(reason)
        lines += [
            closed_at_line(exit_fill.executed_at, tz_offset_hours),
            f"Выход: {fmt_price(exit_fill.price)} · объём {fmt_qty(exit_fill.quantity)}",
            f"Комиссия выхода: {fmt_amount(exit_fill.fee)} {asset}",
        ]
        if trade.status is TradeStatus.CLOSED and trade.pnl is not None:
            lines.append(
                f"PnL: {fmt_money(trade.pnl)} {asset} · комиссии вход+выход "
                f"{fmt_amount(trade.fees)} {asset}"
            )
            lines.append(f"📒 Сделка #{trade.id} закрыта в журнале")
        else:
            lines.append(f"📒 Сделка #{trade.id} остаётся открытой")
        return "\n".join(lines)

    # --- Вход с неизвестным исходом ---------------------------------------

    async def _resolve_unresolved_entry(
        self,
        ctx: _UserCtx,
        client: ExchangeClient,
        entry: ExecutionOrder,
        positions: list[Position],
    ) -> None:
        trades = TradeRepository(ctx.session)
        # С исполнениями: подтверждение правит исполнение входа.
        trade = await trades.lock_for_reconcile(entry.trade_id) if entry.trade_id else None
        if entry.status is OrderStatus.SUBMITTED and trade is not None and trade.fill_confirmed:
            return  # исполнение уже в журнале — разбирать нечего
        snapshot = UnresolvedEntry(
            execution_order_id=entry.id,
            client_order_id=entry.client_order_id or "",
            symbol=entry.symbol,
            side=entry.position_side,
            status=entry.status.value,
            created_at=entry.created_at,
            trade_id=entry.trade_id,
        )
        if not entry_is_due(snapshot, ctx.now):
            return
        lookup: OrderFill | None = None
        not_found = False
        try:
            lookup = await client.get_order_fill(entry.symbol, snapshot.client_order_id)
        except ExchangeError as exc:
            if exc.code != ORDER_NOT_EXIST_CODE:
                logger.warning(
                    "Поиск входа по client_order_id не удался — повторю на следующем цикле",
                    extra={"execution_order_id": entry.id, "code": exc.code},
                )
                return
            not_found = True
        position = exchange_position(positions, entry.symbol, entry.position_side)
        resolution = resolve_entry(snapshot, lookup, not_found=not_found, position=position)

        if resolution.discrepancy is not None:
            await self._discrepancy(ctx, resolution.discrepancy)
            return
        await self._resolve_missing(
            ctx, ReconciliationKind.AMBIGUOUS, set(), prefix=f"entry:{entry.id}:"
        )
        if resolution.confirmed is not None:
            await self._confirm_entry(ctx, entry, trade, resolution.confirmed)
        elif resolution.not_placed:
            entry.status = OrderStatus.NOT_PLACED
            note = ""
            if trade is not None and trade.status is TradeStatus.OPEN and not trade.fill_confirmed:
                await TradeJournal(trades).cancel_trade(trade)
                note = f"\nПредварительная сделка #{trade.id} отменена в журнале."
            await self._fact(
                ctx,
                ReconciliationEvent(
                    user_id=ctx.user_id, trade_id=entry.trade_id, execution_order_id=entry.id,
                    symbol=entry.symbol, kind=ReconciliationKind.ENTRY_NOT_PLACED,
                    dedup_key=f"entry:{entry.id}:not_placed",
                    detail=f"{entry.client_order_id}: order not exist, позиции нет",
                ),
                f"ℹ️ Вход {entry.symbol} {entry.position_side.value} не выставлен на бирже "
                f"(проверено через {int(UNRESOLVED_ENTRY_WINDOW.total_seconds() // 60)} мин, "
                f"{entry.client_order_id}). Повторной отправки нет.{note}",
            )

    async def _confirm_entry(
        self, ctx: _UserCtx, entry: ExecutionOrder, trade: Trade | None, fill: OrderFill
    ) -> None:
        """Поиск по client_order_id нашёл вход FILLED: строка входа — FILLED
        (exchange_order_id не перезаписывается), дальше по сделке:

        - OPEN и не подтверждена — исполнение входа и сделка приводятся к
          факту биржи (цена, объём, комиссия, время, positionID), событие
          ENTRY_CONFIRMED;
        - OPEN и не подтверждена, но ENTRY-исполнения в журнале нет —
          AMBIGUOUS entry:{id}:no_entry_fill, не пересчитываем (пустые fills
          обнулили бы цену и объём) и не подтверждаем (решение 29.09, B4);
        - сделки нет — AMBIGUOUS entry:{id}:no_trade, сделку не создаём. Вход
          уже FILLED и в следующие циклы не попадает — расхождение остаётся
          открытым навсегда: так задумано (решение 29.09), разбирает владелец;
        - сделка CANCELLED/CLOSED или уже подтверждена — журнал не трогаем,
          только событие-факт ENTRY_CONFIRMED: так задумано (решение 29.09)."""
        entry.status = OrderStatus.FILLED
        entry.exchange_order_id = entry.exchange_order_id or fill.order_id
        text = (
            f"✅ Вход {entry.symbol} {entry.position_side.value} найден на бирже: "
            f"{fmt_decimal(fill.executed_qty)} по {fmt_decimal(fill.avg_price)}"
        )
        if trade is not None and trade.status is TradeStatus.OPEN and not trade.fill_confirmed:
            entry_fill = next(
                (f for f in trade.fills if f.fill_side is FillSide.ENTRY), None
            )
            if entry_fill is None:
                await self._discrepancy(
                    ctx,
                    Discrepancy(
                        ReconciliationKind.AMBIGUOUS, f"entry:{entry.id}:no_entry_fill",
                        entry.symbol,
                        f"Сделка #{trade.id} без исполнения входа в журнале — подтверждение "
                        "не выполнено, проверь вручную",
                        trade_id=trade.id, execution_order_id=entry.id,
                    ),
                )
                return
            entry_fill.price = fill.avg_price
            entry_fill.quantity = fill.executed_qty
            entry_fill.fee = fill.fee
            entry_fill.external_fill_id = fill.order_id
            if fill.filled_at is not None:
                entry_fill.executed_at = fill.filled_at
                trade.opened_at = fill.filled_at
            trade.fill_confirmed = True
            raw_position = fill.raw.get("positionID") or fill.raw.get("positionId")
            if raw_position not in (None, "", 0, "0"):
                trade.external_position_id = str(raw_position)
            TradeJournal(TradeRepository(ctx.session)).recalculate(trade)
            text += f"\n📒 Сделка #{trade.id} подтверждена фактом биржи."
        elif trade is None:
            await self._discrepancy(
                ctx,
                Discrepancy(
                    ReconciliationKind.AMBIGUOUS, f"entry:{entry.id}:no_trade", entry.symbol,
                    f"вход {entry.client_order_id} исполнен на бирже, сделки в журнале нет — "
                    "не создаю",
                    execution_order_id=entry.id,
                ),
            )
            return
        await self._fact(
            ctx,
            ReconciliationEvent(
                user_id=ctx.user_id, trade_id=entry.trade_id, execution_order_id=entry.id,
                symbol=entry.symbol, kind=ReconciliationKind.ENTRY_CONFIRMED,
                dedup_key=f"entry:{entry.id}:confirmed",
                detail=f"{entry.client_order_id}: FILLED {fill.executed_qty} по {fill.avg_price}",
            ),
            text,
        )

    # --- События ----------------------------------------------------------

    async def _fact(self, ctx: _UserCtx, event: ReconciliationEvent, text: str) -> None:
        """Событие-факт (журнал уже приведён к бирже): сразу разрешено,
        уведомление одно — повтор невозможен, сам факт записан один раз."""
        event.resolved_at = ctx.now
        event.notify_text = text
        self.pulse.log_events += 1
        ReconciliationEventRepository(ctx.session).add(event)
        await ctx.session.flush()
        logger.info(
            "Сверка: факт биржи записан",
            extra={"kind": event.kind.value, "trade_id": event.trade_id, "symbol": event.symbol},
        )
        await self._attempt(event, ctx.telegram_id, ctx.now, text)

    async def _discrepancy(
        self,
        ctx: _UserCtx,
        found: Discrepancy,
        *,
        alarm: bool = False,
        resolve_now: bool = False,
    ) -> None:
        """Расхождение без однозначного факта: журнал не правится, одно
        уведомление на открытое расхождение (дедуп по dedup_key)."""
        repo = ReconciliationEventRepository(ctx.session)
        ctx.active_keys.add(found.dedup_key)
        if await repo.get_open(ctx.user_id, found.dedup_key) is not None:
            return
        if alarm:
            text = (
                f"⚠️ ПОЗИЦИЯ БЕЗ СТОПА: {found.symbol} — {found.detail}. Бот ордеров не "
                "ставит — выставь стоп в BingX."
            )
        else:
            text = (
                f"⚠️ Сверка с биржей, {found.symbol}: {found.detail}. Журнал не изменён — "
                "проверь BingX."
            )
        event = repo.add(
            ReconciliationEvent(
                user_id=ctx.user_id, trade_id=found.trade_id,
                execution_order_id=found.execution_order_id, symbol=found.symbol,
                kind=found.kind, dedup_key=found.dedup_key, detail=found.detail,
                resolved_at=ctx.now if resolve_now else None, notify_text=text,
            )
        )
        await repo.flush()
        self.pulse.log_events += 1
        logger.info(
            "Сверка: расхождение",
            extra={"kind": found.kind.value, "dedup_key": found.dedup_key, "symbol": found.symbol},
        )
        await self._attempt(event, ctx.telegram_id, ctx.now, text)

    async def _resolve_missing(
        self,
        ctx: _UserCtx,
        kind: ReconciliationKind,
        active: set[str],
        *,
        prefix: str | None = None,
    ) -> None:
        """Открытые расхождения вида kind, которых на этом цикле больше нет
        (prefix — только ключи одной сделки/входа), — разрешены.

        prefix кончается двоеточием ("entry:5:"): он покрывает сам ключ
        "entry:5" и ключи "entry:5:…", но не "entry:52" (п.8, 29.09 — без
        двоеточия вход 1 разрешал расхождения входа 12)."""
        for event in await ReconciliationEventRepository(ctx.session).list_open(
            ctx.user_id, [kind]
        ):
            if prefix is not None and not _in_scope(event.dedup_key, prefix):
                continue
            if event.dedup_key not in active:
                event.resolved_at = ctx.now

    async def _resolve_trade_discrepancies(self, ctx: _UserCtx, trade_id: int) -> None:
        for kind, prefix in (
            (ReconciliationKind.QUANTITY_MISMATCH, f"qty:{trade_id}:"),
            (ReconciliationKind.AMBIGUOUS, f"ambiguous:{trade_id}:"),
            (ReconciliationKind.AMBIGUOUS, f"history:{trade_id}:"),
        ):
            await self._resolve_missing(ctx, kind, set(), prefix=prefix)


def _redelivery_text(event: ReconciliationEvent, user: User, now: datetime) -> str:
    """Текст переотправки: сохранённый notify_text (у событий до 28.09 его
    нет — собираем из символа и detail) и, если событие старше 2 мин, первой
    строкой «⏱ Событие от HH:MM (доставлено с опозданием)» в поясе
    пользователя."""
    body = event.notify_text or (
        f"⚠️ Сверка с биржей, {event.symbol}: {event.detail}. Проверь BingX."
    )
    timezone = user.settings.timezone if user.settings is not None else None
    notice = late_notice(
        created_at=event.created_at, now=now, tz_offset_hours=tz_offset_for(timezone)
    )
    return f"{notice}\n{body}" if notice else body


def _in_scope(dedup_key: str, prefix: str) -> bool:
    """dedup_key принадлежит записи prefix ("kind:id:"): равен "kind:id" или
    начинается с "kind:id:"."""
    if not prefix.endswith(":"):
        raise ValueError(f"Префикс dedup_key без двоеточия: {prefix!r}")
    return dedup_key == prefix[:-1] or dedup_key.startswith(prefix)


class _UserCtx:
    """Состояние одного прохода по пользователю."""

    def __init__(
        self,
        session: AsyncSession,
        telegram_id: int,
        user_id: int,
        asset: str,
        now: datetime,
        tz_offset_hours: int = 5,
    ) -> None:
        self.session = session
        self.telegram_id = telegram_id
        self.user_id = user_id
        self.asset = asset
        self.now = now
        # Пояс пользователя — время исполнения в уведомлениях (29.09).
        self.tz_offset_hours = tz_offset_hours
        self.active_keys: set[str] = set()

