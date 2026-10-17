"""Открытие сделки из Mini App (§3, §5а): preview, карточка, «Открыть»,
«Отменить лимит», статус. Вся торговая логика — ядро (OpeningService), здесь
только перевод ввода, ответы и итог в чат.

preview — на кэше процесса (PreviewFactory): живой пересчёт до 5/с не должен
упираться в лимиты BingX (marginType — 2/с). Карточка (POST /api/open) и
«Открыть» — на свежих данных биржи.

«Открыть» — confirm ядра в своей задаче процесса API. Запрос ждёт итог до
CONFIRM_WAIT_SECONDS; дольше (лимит, поиск по cid) — отвечает текущим
статусом, задача доживает сама, фронт опрашивает GET /api/open/{id}. У задачи
своя сессия БД — от запроса не зависит. Если процесс API упадёт посреди
confirm, открытие довосстановит цикл бота (состояние — в trade_openings).

Итог — в чат тем же сообщением, что у цикла открытий (send_opening_result,
журнал исходящих); тревога ALARM — сразу быстрые повторы защиты B.1 тем же
кодом, что в боте (OpeningsWorker.retry_alarm, лок позиции общий).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api import positions as positions_api
from app.api.deps import ApiError, ApiState
from app.api.market import TTL_LEVERAGE, TTL_PRICE, MarketReader
from app.api.schemas import (
    CalcOut,
    CardOut,
    IssueOut,
    LevelsOut,
    OpenIn,
    OpeningOut,
    OpenOut,
    RefusalOut,
    RoundedOut,
    money,
    price,
    qty,
)
from app.bot import opening_messages
from app.bot.opening_result import result_position, send_opening_result, strip_markup
from app.core.logging import get_logger
from app.database.models.execution_order import ExecutionOrder
from app.database.models.outgoing_message import OutgoingMeta
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.execution.callback_audit import record_callback
from app.execution.opening.calc import (
    LevelInput,
    LevelKind,
    RawOpeningInputs,
    RiskInput,
    RiskKind,
    take_at_r,
)
from app.execution.opening.service import (
    LIMIT_EXPIRY_CHOICES,
    STATUS_TEXT,
    CardOutcome,
    ConfirmOutcome,
    OpeningService,
)
from app.trading.enums import (
    OPENING_ACTIVE,
    EntryType,
    ExecutionCallbackAction,
    OpeningSource,
    OpeningStatus,
    OrderRole,
    OrderStatus,
    TradeSide,
)

logger = get_logger(__name__)

CONFIRM_WAIT_SECONDS = 20.0
# Итог, который в чат не уходит: открытия не было, Mini App покажет сам.
NOT_TO_CHAT = (OpeningStatus.REFUSED, OpeningStatus.EXPIRED_CARD)
# Дальше статус сам не изменится (CARD и активные — изменится).
NOT_FINAL = (OpeningStatus.CARD, *OPENING_ACTIVE)
AUDIT_FAILED_TEXT = "Нажатие не записано — на биржу ничего не отправлено. Повтори."
NOT_FOUND_TEXT = "Открытие не найдено."

# Кэш превью: цена и позиции — как у экрана рынка (2 с), плечо — 30 с (тот же
# ключ, что у /api/market), режим маржи — 30 с, баланс — 5 с.
PREVIEW_TTL = {
    "get_ticker": TTL_PRICE,
    "get_positions": TTL_PRICE,
    "get_open_orders": TTL_PRICE,
    "get_balance": 5,
    "get_margin_type": 30,
    "get_leverage": TTL_LEVERAGE,
}


class PreviewReads:
    """Клиент превью: только чтение (get_*), частые — через кэш процесса.
    Запись на биржу из превью невозможна — AttributeError."""

    def __init__(self, client: Any, state: ApiState, user_id: int) -> None:
        self._client = client
        self._cache = state.market_cache
        self._prefix = f"{user_id}:{state.settings.bingx_allowed_exchange_mode.value}"

    def _key(self, name: str, args: tuple[Any, ...]) -> str:
        if name == "get_leverage":
            return f"api:leverage:{self._prefix}:{args[0]}"   # общий с MarketReader
        return f"api:pv:{name}:{self._prefix}:{':'.join(map(str, args))}"

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._client, name)
        if not callable(method):
            return method                                  # name и прочие поля клиента
        if not name.startswith("get_"):
            raise AttributeError(f"превью не пишет на биржу: {name}")
        ttl = PREVIEW_TTL.get(name)
        if ttl is None:
            return method

        async def cached(*args: Any, **kwargs: Any) -> Any:
            return await self._cache.get_or_fetch(
                self._key(name, args), ttl, lambda: method(*args, **kwargs)
            )

        return cached

    async def close(self) -> None:
        await self._client.close()


class PreviewFactory:
    def __init__(self, state: ApiState) -> None:
        self._state = state

    async def get_credentials(self, session: Any, user_id: int, **kwargs: Any) -> Any:
        return await self._state.factory.get_credentials(session, user_id, **kwargs)

    async def for_user(self, session: Any, user_id: int, **kwargs: Any) -> PreviewReads:
        client = await self._state.factory.for_user(session, user_id, **kwargs)
        return PreviewReads(client, self._state, user_id)


def service_for(
    state: ApiState, session: Any, user: Any, *, preview: bool = False
) -> OpeningService:
    factory = PreviewFactory(state) if preview else state.factory
    return OpeningService(session, state.settings, None, user, redis=state.redis,
                          factory=factory, market_cache=state.market_cache)


# --- ввод и ответы ------------------------------------------------------------------


def raw_inputs(body: OpenIn, default_expiry: int) -> RawOpeningInputs:
    is_limit = body.entry_type == "LIMIT"
    if is_limit and body.limit_price is None:
        raise ApiError(422, "BAD_REQUEST", "Для лимита нужна цена лимита.")
    expiry = body.expiry_minutes if body.expiry_minutes is not None else default_expiry
    if is_limit and expiry not in LIMIT_EXPIRY_CHOICES:
        raise ApiError(422, "BAD_REQUEST", "Срок лимита — 15 мин, 1 ч, 4 ч или 24 ч.")
    return RawOpeningInputs(
        symbol=body.symbol, side=TradeSide(body.side), entry_type=EntryType(body.entry_type),
        stop=LevelInput(LevelKind(body.stop.kind), body.stop.value),
        take=LevelInput(LevelKind(body.take.kind), body.take.value) if body.take else None,
        risk=RiskInput(RiskKind(body.risk.kind), body.risk.value),
        leverage=body.leverage,
        limit_price=body.limit_price if is_limit else None,
        expiry_minutes=expiry if is_limit else None,
    )


def card_out(outcome: CardOutcome) -> CardOut:
    info = outcome.market.symbol_info if outcome.market is not None else None
    pp = info.price_precision if info else None
    qp = info.quantity_precision if info else None
    calc = levels = None
    if outcome.calc is not None and outcome.inputs is not None and info is not None:
        assert pp is not None and qp is not None
        c, i = outcome.calc, outcome.inputs
        calc = CalcOut(
            entry_price=price(c.entry_price, pp), quantity=qty(c.quantity, qp),
            notional=money(c.notional), risk_usd=money(c.risk_usd),
            risk_percent=money(c.risk_percent), fee_usd=money(c.fee_usd),
            margin=money(c.margin), liq_estimate=price(c.liq_estimate, pp),
            liq_ratio=money(c.liq_ratio), distance_percent=money(c.distance_percent),
            rr=money(c.rr), max_leverage_for_stop=c.max_leverage_for_stop,
            suggested_leverage=c.suggested_leverage, fills_immediately=c.fills_immediately,
            take_2r=take_at_r(c.entry_price, i.stop_loss, i.side, 2, pp),
        )
        levels = LevelsOut(
            entry_type=i.entry_type.value, stop_loss=price(i.stop_loss, pp),
            take_profit=price(i.take_profit, pp), limit_price=price(i.limit_price, pp),
            risk_percent=i.risk_percent, leverage=i.leverage, expiry_minutes=i.expiry_minutes,
        )
    return CardOut(
        text=outcome.text, can_open=outcome.can_open, has_warnings=outcome.has_warnings,
        issues=[IssueOut(code=x.code, level=x.level.value, message=x.message)
                for x in outcome.issues],
        refusal=(RefusalOut(code=outcome.refusal.code.value, message=outcome.refusal.message)
                 if outcome.refusal is not None else None),
        calc=calc, levels=levels,
        rounded=[RoundedOut(field=r.field, label=r.label, before=r.before,
                            after=price(r.after, pp) if pp is not None else r.after)
                 for r in outcome.rounded],
        price_precision=pp, quantity_precision=qp,
    )


def _status_text(opening: TradeOpening) -> str:
    if opening.status is OpeningStatus.DONE and opening.trade_id is not None:
        return f"✅ Открыто — сделка #{opening.trade_id}."
    if opening.status is OpeningStatus.CARD:
        return "Карточка ещё не подтверждена."
    text = STATUS_TEXT.get(opening.status, opening.status.value)
    if opening.error_message and opening.status not in (
        OpeningStatus.CARD, OpeningStatus.DONE
    ):
        text = f"{text} {opening.error_message}"
    return text


async def opening_out(
    state: ApiState, session: Any, opening: TradeOpening, *, text: str | None = None,
    precision: tuple[int, int] | None = None,
) -> OpeningOut:
    """Статус открытия для Mini App. Точность — из списка контрактов (кэш 1 ч)."""
    if precision is None:
        async with MarketReader(state) as reader:
            info = await reader.symbol(opening.symbol)
        precision = (info.price_precision, info.quantity_precision) if info else (8, 8)
    pp, qp = precision
    placed = set(await session.scalars(
        select(ExecutionOrder.role).where(
            ExecutionOrder.trade_opening_id == opening.id,
            ExecutionOrder.role.in_((OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT)),
            ExecutionOrder.status == OrderStatus.SUBMITTED,
        )
    ))
    expires = opening.expires_at
    if opening.status is OpeningStatus.CARD:
        expires = opening.created_at + timedelta(seconds=state.settings.exec_open_card_ttl_seconds)
    return OpeningOut(
        id=opening.id, status=opening.status.value, final=opening.status not in NOT_FINAL,
        text=text or _status_text(opening), symbol=opening.symbol, side=opening.side.value,
        entry_type=opening.entry_type.value,
        quantity=qty(opening.filled_qty or opening.quantity, qp),
        avg_price=price(opening.avg_price, pp), limit_price=price(opening.limit_price, pp),
        stop_loss=price(opening.stop_loss, pp),
        take_profit=price(opening.take_profit, pp), liq_estimate=price(opening.liq_estimate, pp),
        stop_placed=OrderRole.STOP_LOSS in placed, take_placed=OrderRole.TAKE_PROFIT in placed,
        trade_id=opening.trade_id, expires_at=expires,
        error_code=opening.error_code, error_message=opening.error_message,
    )


async def owned(service: OpeningService, opening_id: int) -> TradeOpening:
    """Чужое и несуществующее — одинаково 404 (существование не подтверждаем)."""
    opening = await service.load(opening_id)
    if opening is None:
        raise ApiError(404, "NOT_FOUND", NOT_FOUND_TEXT)
    return opening


async def audit(
    state: ApiState, user: User, action: ExecutionCallbackAction, opening_id: int, raw: str
) -> None:
    """Журнал нажатий (как у кнопок чата): не записалось — действие не выполняется."""
    try:
        await record_callback(
            state.db, user_id=user.id, telegram_id=user.telegram_id, action=action,
            notification_id=None, trade_opening_id=opening_id, raw_data=raw,
            chat_id=None, message_id=None, callback_query_id=None,
        )
    except Exception:
        logger.exception("Нажатие Mini App не записано",
                         extra={"user_id": user.id, "opening_id": opening_id})
        raise ApiError(503, "AUDIT_FAILED", AUDIT_FAILED_TEXT) from None


# --- карточка -------------------------------------------------------------------------


async def open_card(state: ApiState, session: Any, user: User, body: OpenIn) -> OpenOut:
    """CARD (или REFUSED) со свежими данными биржи; прежние карточки — и в чате,
    и в Mini App — больше не действуют (кнопки в чате снимаются)."""
    inputs = raw_inputs(body, state.settings.exec_open_limit_expiry_minutes)
    service = service_for(state, session, user)
    outcome = await service.prepare(inputs, source=OpeningSource.MINIAPP)
    opening = outcome.opening
    stale = await service.supersede_cards(opening.id if opening else None)
    if stale and state.bot is not None:
        for chat_id, message_id in stale:
            await strip_markup(state.bot, chat_id, message_id)
    expires = (
        opening.created_at + timedelta(seconds=state.settings.exec_open_card_ttl_seconds)
        if opening is not None and opening.status is OpeningStatus.CARD else None
    )
    return OpenOut(
        opening_id=opening.id if opening else None,
        status=opening.status.value if opening else None,
        expires_at=expires, card=card_out(outcome),
    )


# --- «Открыть» ------------------------------------------------------------------------


async def _confirm(
    state: ApiState, user_id: int, opening_id: int, accept_warnings: bool
) -> ConfirmOutcome:
    async with state.db.session() as session:
        # Ядро читает user.settings (гварды) — сразу, без ленивой загрузки.
        user = await session.scalar(
            select(User).where(User.id == user_id).options(selectinload(User.settings))
        )
        if user is None:
            return ConfirmOutcome("Пользователь не найден.", final=False)
        service = service_for(state, session, user)
        outcome = await service.confirm(opening_id, accept_warnings=accept_warnings)
        opening = await service.load(opening_id)
        telegram_id = user.telegram_id
    if outcome.final and outcome.status is not None and outcome.status not in NOT_TO_CHAT:
        position = (
            result_position(outcome, opening.symbol, opening.side) if opening else None
        )
        meta = OutgoingMeta(user_id=user_id, kind=f"OPEN_{outcome.status.value}",
                            trade_opening_id=opening_id, trade_id=outcome.trade_id)
        try:
            if state.bot is None:
                raise RuntimeError("Bot не создан")
            await send_opening_result(
                state.bot, state.db, telegram_id, outcome.text, status=outcome.status,
                opening_id=opening_id, position=position, meta=meta,
            )
        except Exception:
            # Вход уже сделан: ошибка доставки не должна превращаться в ошибку
            # открытия — итог виден в Mini App и в «Позициях».
            logger.exception("Итог открытия из Mini App не отправлен в чат",
                             extra={"opening_id": opening_id})
    # Позиция могла появиться (или закрыться аварийно): «Позиции» и линии — заново.
    positions_api.forget(state, user_id)
    if outcome.status is OpeningStatus.ALARM and state.alarm_retry is not None:
        # B.1: не ждать цикла бота 15 с; после записи тревоги в журнал —
        # «✅ Решено» ищет её там.
        state.alarm_retry(opening_id)
    return outcome


def _forget(state: ApiState, opening_id: int, task: asyncio.Task[ConfirmOutcome]) -> None:
    if state.confirm_tasks.get(opening_id) is task:
        del state.confirm_tasks[opening_id]
    if not task.cancelled() and task.exception() is not None:
        logger.error("Открытие из Mini App упало", exc_info=task.exception(),
                     extra={"opening_id": opening_id})


async def confirm(
    state: ApiState, user_id: int, opening_id: int, accept_warnings: bool,
    *, wait_seconds: float = CONFIRM_WAIT_SECONDS,
) -> ConfirmOutcome | None:
    """Итог confirm или None — ещё идёт (ответить статусом, фронт опрашивает).
    Повторный запрос по тому же открытию ждёт ту же задачу, второй confirm не
    запускается (ядро всё равно отсекло бы его переходом CARD → CONFIRMED)."""
    task = state.confirm_tasks.get(opening_id)
    if task is None or task.done():
        task = asyncio.create_task(_confirm(state, user_id, opening_id, accept_warnings))
        state.confirm_tasks[opening_id] = task
        task.add_done_callback(lambda t: _forget(state, opening_id, t))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
    except TimeoutError:
        return None


# --- «Отменить лимит» -----------------------------------------------------------------


async def cancel(
    state: ApiState, service: OpeningService, opening: TradeOpening
) -> ConfirmOutcome:
    """Та же ветка ядра, что у кнопки в чате; ⏳ лимита в чате правится итогом
    (кнопка «Отменить лимит» под ним снимается)."""
    if opening.status is not OpeningStatus.WORKING:
        raise ApiError(409, "NOT_WORKING", _status_text(opening))
    outcome = await service.cancel_limit(opening.id)
    positions_api.forget(state, opening.user_id)   # лимит мог успеть частично исполниться
    if outcome.final and state.bot is not None:
        try:
            await opening_messages.retire_working(state.bot, state.db, opening.id,
                                                  outcome.status)
        except Exception:
            logger.exception("⏳ лимита не исправлен после отмены из Mini App",
                             extra={"opening_id": opening.id})
    return outcome


__all__ = [
    "CONFIRM_WAIT_SECONDS",
    "PreviewFactory",
    "PreviewReads",
    "audit",
    "cancel",
    "card_out",
    "confirm",
    "open_card",
    "opening_out",
    "owned",
    "raw_inputs",
    "service_for",
]
