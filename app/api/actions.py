"""Действия с позицией из Mini App (экран 5, 5.1): экран действий, превью, карточка,
«Да» / «Нет», статус. Вся логика — PositionActionService (тот же, что у кнопок
чата): расчёт, проверки, лок позиции, перечитывание биржи на «Да», _drift, потолок
риска, cid. Здесь — перевод ввода, ответы, журнал нажатий и итог в чат.

Выключатель MINIAPP_ACTIONS_ENABLED (по умолчанию false): каждый эндпоинт первым
делом отвечает 403 — ни биржи, ни строк.

Экран действий и превью — на кэше процесса (PreviewFactory, только get_*), карточка
и «Да» — на свежих данных биржи. Карточка — строка CARD с card_message_id NULL:
«Да» из чата по ней не пройдёт («Это не последняя карточка»), «Да» из Mini App по
карточке чата — тоже. Вытеснения нет (решение владельца 25.10): исполнилась одна
карточка позиции — «Да» на другой отказывает в _drift, на биржу ничего.

«Да» — confirm ядра в своей задаче (state.action_tasks, не общий с открытиями):
запрос ждёт итог до CONFIRM_WAIT_SECONDS, дальше отвечает pending, задача доживает
сама. Повторный запрос ждёт ту же задачу. Процесс API упал посреди «Да» —
действие доводит восстановление в цикле бота (B.9, ACTION_RECOVERED в чат).

Итог в чат — новым сообщением ACTION_RESULT с «⬅️ К позициям», ровно один раз:
только если это «Да» само перевело карточку из CARD в DONE / FAILED / DRY_RUN.
Отказ и устаревшая карточка в чат не идут — их показывает Mini App.

Журнал нажатий — те же pm_open / pm_yes / pm_yes_risk / pm_no, что у кнопок чата,
raw_data «ma:pa:<код>:<SYMBOL>:<L|S>» / «ma:pm:<y|r|n>:<id>», без чата и сообщения.
Не записалось — 503, действие не выполняется.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api import positions as positions_api
from app.api.deps import ApiError, ApiState
from app.api.opening import AUDIT_FAILED_TEXT, PreviewFactory
from app.api.schemas import (
    ActionCardOut,
    ActionHintOut,
    ActionIn,
    ActionOpenOut,
    ActionRowOut,
    ActionStatusOut,
    PositionActionsOut,
    RefusalOut,
    price,
)
from app.bot import outbox
from app.bot.action_result import to_positions
from app.core.logging import get_logger
from app.database.models.outgoing_message import OutgoingMessage, OutgoingMeta
from app.database.models.position_action import PositionAction
from app.database.models.user import User
from app.exchanges.base import ExchangeError
from app.execution.callback_audit import record_callback
from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.position_action_service import (
    CARD_EXPIRED_TEXT,
    SUBMITTED_TEXT,
    CardOutcome,
    ConfirmOutcome,
    PositionActionService,
)
from app.execution.position_actions import (
    R_STEP,
    ActionInputs,
    breakeven_price,
    entry_fee_unit,
    plan_action,
)
from app.execution.position_view import PositionView
from app.trading.enums import (
    ExecutionCallbackAction,
    PositionActionKind,
    PositionActionStatus,
    TradeSide,
)

logger = get_logger(__name__)

Kind = PositionActionKind
Status = PositionActionStatus

CONFIRM_WAIT_SECONDS = 20.0
DISABLED_TEXT = "Действия в Mini App выключены — открой «⚙️ Действия в чате»."
NOT_FOUND_TEXT = "Действие не найдено."
RESULT_KIND = "ACTION_RESULT"
# Итог, который уходит в чат: на биржу что-то ушло (или ушло бы — сухой прогон).
TO_CHAT = (Status.DONE, Status.FAILED, Status.DRY_RUN)
# Дальше статус сам не изменится (CARD и SUBMITTED — изменится).
NOT_FINAL = (Status.CARD, Status.SUBMITTED)

# Код кнопки → (вид, параметры); цена стопа/тейка — из тела запроса.
CODES: dict[str, tuple[PositionActionKind, dict[str, object]]] = {
    "be": (Kind.MOVE_STOP, {"breakeven": True}),
    "sl": (Kind.MOVE_STOP, {}),
    "tp": (Kind.SET_TAKE, {}),
    "c25": (Kind.CLOSE_PARTIAL, {"fraction": "25"}),
    "c50": (Kind.CLOSE_PARTIAL, {"fraction": "50"}),
    "cf": (Kind.CLOSE_FULL, {}),
}
SIDE_CODE = {TradeSide.LONG: "L", TradeSide.SHORT: "S"}

STATUS_TEXT = {
    Status.CARD: "Карточка ещё не подтверждена.",
    Status.SUBMITTED: SUBMITTED_TEXT,
    Status.DECLINED: "Отменено — на биржу ничего не отправлено.",
    Status.EXPIRED: CARD_EXPIRED_TEXT,
    Status.DONE: "✅ Выполнено.",
    Status.DRY_RUN: "🧪 Сухой прогон (EXEC_DRY_RUN): на биржу ничего не ушло.",
}

_TAG = re.compile(r"<[^>]+>")


def plain_text(text: str) -> str:
    """Текст сообщения чата для Mini App: без тегов, сущности раскодированы
    (в журнале исходящих — HTML Telegram: «&amp;», «&lt;»)."""
    return html.unescape(_TAG.sub("", text))


def require_enabled(state: ApiState) -> None:
    if not state.settings.miniapp_actions_enabled:
        raise ApiError(403, "ACTIONS_DISABLED", DISABLED_TEXT)


def _unavailable(exc: ExchangeError) -> ApiError:
    return ApiError(503, "EXCHANGE_UNAVAILABLE",
                    f"Биржа не ответила ({type(exc).__name__}) — повтори.")


def service_for(
    state: ApiState, session: Any, user: User, *, preview: bool = False
) -> PositionActionService:
    factory = PreviewFactory(state) if preview else state.factory
    return PositionActionService(session, state.settings, None, user, redis=state.redis,
                                 factory=factory, market_cache=state.market_cache)


def params_for(body: ActionIn) -> tuple[PositionActionKind, dict[str, object]]:
    kind, base = CODES[body.kind]
    params = dict(base)
    if body.level is not None:
        params["level"] = body.level
    elif body.level_percent is not None:
        params["level_percent"] = body.level_percent
    return kind, params


def code_of(action: PositionAction) -> str:
    params = action.params or {}
    if action.kind is Kind.MOVE_STOP:
        return "be" if params.get("breakeven") else "sl"
    if action.kind is Kind.SET_TAKE:
        return "tp"
    if action.kind is Kind.CLOSE_PARTIAL:
        return f"c{params.get('fraction')}"
    return "cf"


def _refusal(refusal: ExecutionRefusal | None) -> RefusalOut | None:
    return RefusalOut(code=refusal.code.value, message=refusal.message) if refusal else None


# --- карточка: строки -----------------------------------------------------------------


def card_out(outcome: CardOutcome) -> ActionCardOut:
    """Строки — значения CardView (одно место форматирования, card_view): те же
    числа, что в тексте карточки чата."""
    v = outcome.view
    if v is None:
        return ActionCardOut(text=outcome.text, refusal=_refusal(outcome.refusal), title=None,
                             rows=[], notes=[], risk_increase=False, new_level=None)
    rows = [("Позиция", v.quantity), ("Вход", v.entry), ("Mark", v.mark)]
    notes: list[str] = []
    if v.kind in (Kind.MOVE_STOP, Kind.SET_TAKE):
        name = "Стоп" if v.kind is Kind.MOVE_STOP else "Тейк"
        rows.append((name, f"{v.old_level} → {v.new_level}"))
        notes.append(f"{name} — на всю позицию")
        if v.pnl is not None:
            rows.append(("PnL по тейку", f"≈ {v.pnl} USDT"))
    else:
        rows.append((f"Закрыть {v.close_what}", f"{v.close_qty} маркетом"))
        rows.append(("Комиссия", f"≈{v.fee} USDT"))
        if v.pnl is not None:
            rows.append(("PnL по mark", f"≈ {v.pnl} USDT"))
        if v.remainder is not None:
            notes.append(v.remainder)
    if v.risk is not None:
        if v.risk.increase:
            delta = f" ({v.risk.delta})" if v.risk.delta else ""
            rows.append(("Риск увеличится", f"{v.risk.before} → {v.risk.after}{delta}"))
        else:
            rows.append(("Риск", f"{v.risk.before} → {v.risk.after}"))
        if v.kind is Kind.MOVE_STOP:
            rows.append(("Комиссия выхода по стопу", f"≈{v.fee} USDT"))
            notes.append("Комиссия выхода учтена в риске")
        if v.breakeven:
            notes.append("≈0 нетто без учёта проскальзывания (стоп исполняется маркетом)")
    if v.risk_share is not None:
        rows.append(("Риск после", v.risk_share))
    return ActionCardOut(
        text=outcome.text, refusal=None, title=f"{v.symbol} {v.side} · {v.title}",
        rows=[ActionRowOut(label=label, value=value) for label, value in rows],
        notes=notes, risk_increase=outcome.risk_increase, new_level=v.new_level,
    )


# --- экран действий ---------------------------------------------------------------------


def pnl_r_text(inputs: ActionInputs) -> str | None:
    """PnL позиции в R (результат: прибыль «+», убыток «-», ноль без знака)."""
    p = inputs.position
    if not inputs.one_r_per_unit or p.unrealized_pnl is None or p.quantity <= 0:
        return None
    r = (p.unrealized_pnl / (inputs.one_r_per_unit * p.quantity)).quantize(
        R_STEP, rounding=ROUND_HALF_UP
    )
    if r.is_zero():
        return f"{r.copy_abs()}R"
    return f"{'+' if r > 0 else ''}{r}R"


def _hint(code: str, inputs: ActionInputs) -> ActionHintOut:
    """План ядра без ввода. У стопа и тейка цены ещё нет — «нужна цена» не отказ;
    остальные отказы (несколько стопов, безубыток не достигнут, остаток мал) — да."""
    kind, params = CODES[code]
    plan = plan_action(kind, params, inputs)
    if not isinstance(plan, ExecutionRefusal):
        return ActionHintOut(code=code, enabled=True, hint=None)
    needs_input = code in ("sl", "tp") and plan.code is Code.INVALID_PRICE
    return ActionHintOut(code=code, enabled=needs_input, hint=None if needs_input else plan.message)


async def actions_out(
    state: ApiState, session: Any, user: User, symbol: str, side: TradeSide
) -> PositionActionsOut:
    service = service_for(state, session, user, preview=True)
    try:
        inputs, trade, refusal = await service.read_inputs(symbol, side)
    except ExchangeError as exc:
        raise _unavailable(exc) from None
    if inputs is None:
        assert refusal is not None
        return PositionActionsOut(
            position=None, refusal=_refusal(refusal), pnl_r_text=None, breakeven_price=None,
            actions=[ActionHintOut(code=c, enabled=False, hint=refusal.message) for c in CODES],
        )
    pp = inputs.symbol_info.price_precision
    p = inputs.position
    view = PositionView(p, inputs.stops, inputs.takes, trade)
    be = breakeven_price(p.side, p.entry_price, inputs.fee_rate, entry_fee_unit(inputs), pp)
    return PositionActionsOut(
        position=positions_api.position_out(view, pp), refusal=None,
        pnl_r_text=pnl_r_text(inputs), breakeven_price=price(be, pp),
        actions=[_hint(code, inputs) for code in CODES],
    )


# --- превью и карточка ------------------------------------------------------------------


async def preview(state: ApiState, session: Any, user: User, body: ActionIn) -> ActionCardOut:
    """Живой пересчёт ввода стопа/тейка: без строки действия, на кэше процесса."""
    kind, params = params_for(body)
    try:
        outcome = await service_for(state, session, user, preview=True).preview(
            kind, params, body.symbol, TradeSide(body.side)
        )
    except ExchangeError as exc:
        raise _unavailable(exc) from None
    return card_out(outcome)


async def audit(
    state: ApiState, user: User, action: ExecutionCallbackAction,
    position_action_id: int | None, raw: str,
) -> None:
    """Журнал нажатий (как у кнопок чата): не записалось — действие не выполняется."""
    try:
        await record_callback(
            state.db, user_id=user.id, telegram_id=user.telegram_id, action=action,
            notification_id=None, position_action_id=position_action_id, raw_data=raw,
            chat_id=None, message_id=None, callback_query_id=None,
        )
    except Exception:
        logger.exception("Нажатие Mini App (действие с позицией) не записано",
                         extra={"user_id": user.id, "action_id": position_action_id})
        raise ApiError(503, "AUDIT_FAILED", AUDIT_FAILED_TEXT) from None


def _expires(state: ApiState, action: PositionAction) -> Any:
    return action.created_at + timedelta(seconds=state.settings.exec_confirm_ttl_seconds)


async def open_card(state: ApiState, session: Any, user: User, body: ActionIn) -> ActionOpenOut:
    """CARD (или REFUSED) на свежих данных биржи. Таймер — exec_confirm_ttl_seconds,
    как у карточки в чате."""
    side = TradeSide(body.side)
    await audit(state, user, ExecutionCallbackAction.PM_OPEN, None,
                f"ma:pa:{body.kind}:{body.symbol}:{SIDE_CODE[side]}")
    kind, params = params_for(body)
    try:
        outcome = await service_for(state, session, user).open_card(
            kind, params, body.symbol, side, message_id=None
        )
    except ExchangeError as exc:
        raise _unavailable(exc) from None
    action = outcome.action
    if action is None or outcome.refused:
        return ActionOpenOut(action_id=None, status=Status.REFUSED.value, expires_at=None,
                             ttl_seconds=None, card=card_out(outcome))
    await session.refresh(action)   # created_at — серверный default
    return ActionOpenOut(
        action_id=action.id, status=action.status.value, expires_at=_expires(state, action),
        ttl_seconds=state.settings.exec_confirm_ttl_seconds, card=card_out(outcome),
    )


# --- статус -----------------------------------------------------------------------------


async def owned(service: PositionActionService, action_id: int) -> PositionAction:
    """Чужое, другого счёта и несуществующее — одинаково 404."""
    action = await service.load(action_id)
    if action is None:
        raise ApiError(404, "NOT_FOUND", NOT_FOUND_TEXT)
    return action


async def _text(session: Any, action: PositionAction) -> str:
    if action.status in NOT_FINAL:
        return STATUS_TEXT[action.status]
    sent = await session.scalar(
        select(OutgoingMessage.text).where(
            OutgoingMessage.position_action_id == action.id,
            OutgoingMessage.kind.in_((RESULT_KIND, "ACTION_RECOVERED")),
        ).order_by(OutgoingMessage.id.desc()).limit(1)
    )
    if sent:
        return plain_text(sent)
    if action.status in (Status.REFUSED, Status.FAILED) and action.error_message:
        return f"{'⛔' if action.status is Status.REFUSED else '❌'} {action.error_message}"
    return STATUS_TEXT.get(action.status, action.status.value)


async def status_out(state: ApiState, session: Any, action: PositionAction) -> ActionStatusOut:
    return ActionStatusOut(
        id=action.id, code=code_of(action), kind=action.kind.value, symbol=action.symbol,
        side=action.side.value, status=action.status.value,
        final=action.status not in NOT_FINAL, text=await _text(session, action),
        error_code=action.error_code,
        expires_at=_expires(state, action) if action.status is Status.CARD else None,
        decided_at=action.decided_at,
    )


# --- «Да» ---------------------------------------------------------------------------------


async def _to_chat(state: ApiState, user_id: int, telegram_id: int, action_id: int,
                   text: str) -> None:
    meta = OutgoingMeta(user_id=user_id, kind=RESULT_KIND, position_action_id=action_id)
    try:
        if state.bot is None:
            raise RuntimeError("Bot не создан")
        await outbox.send(state.bot, state.db, meta, telegram_id, text,
                          reply_markup=to_positions())
    except Exception:
        # Действие уже выполнено: сбой доставки — не ошибка действия, итог виден
        # в Mini App и в «Позициях».
        logger.exception("Итог действия из Mini App не отправлен в чат",
                         extra={"action_id": action_id})


async def _confirm(
    state: ApiState, user_id: int, action_id: int, risk_confirmed: bool
) -> ConfirmOutcome:
    async with state.db.session() as session:
        # Ядро читает user.settings (_gate) — сразу, без ленивой загрузки.
        user = await session.scalar(
            select(User).where(User.id == user_id).options(selectinload(User.settings))
        )
        if user is None:
            return ConfirmOutcome("Пользователь не найден.", final=False)
        service = service_for(state, session, user)
        action = await service.load(action_id)
        was_card = action is not None and action.status is Status.CARD
        outcome = await service.confirm(action_id, message_id=None,
                                        risk_confirmed=risk_confirmed)
        after = await service.load(action_id)
        if after is not None:
            await session.refresh(after)
        status = after.status if after is not None else None
        telegram_id = user.telegram_id
    if outcome.final:
        # Позиция могла измениться: «Позиции» и линии — заново.
        positions_api.forget(state, user_id)
    if was_card and status in TO_CHAT:
        await _to_chat(state, user_id, telegram_id, action_id, outcome.text)
    return outcome


def _forget(state: ApiState, action_id: int, task: asyncio.Task[ConfirmOutcome]) -> None:
    if state.action_tasks.get(action_id) is task:
        del state.action_tasks[action_id]
    if not task.cancelled() and task.exception() is not None:
        logger.error("«Да» действия из Mini App упало", exc_info=task.exception(),
                     extra={"action_id": action_id})


async def confirm(
    state: ApiState, user_id: int, action_id: int, risk_confirmed: bool,
    *, wait_seconds: float = CONFIRM_WAIT_SECONDS,
) -> ConfirmOutcome | None:
    """Итог «Да» или None — ещё идёт (ответить статусом, фронт опрашивает GET).
    Повторный запрос по тому же действию ждёт ту же задачу."""
    task = state.action_tasks.get(action_id)
    if task is None or task.done():
        task = asyncio.create_task(_confirm(state, user_id, action_id, risk_confirmed))
        state.action_tasks[action_id] = task
        task.add_done_callback(lambda t: _forget(state, action_id, t))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
    except TimeoutError:
        return None


def flags(outcome: ConfirmOutcome) -> tuple[bool, bool]:
    """(pending, busy) не-итога: SUBMITTED — действие идёт (опрашивать), иначе лок
    позиции занят (повторить через пару секунд)."""
    if outcome.final:
        return False, False
    pending = outcome.text == SUBMITTED_TEXT
    return pending, not pending


__all__ = [
    "CODES",
    "CONFIRM_WAIT_SECONDS",
    "actions_out",
    "audit",
    "card_out",
    "confirm",
    "flags",
    "open_card",
    "owned",
    "params_for",
    "plain_text",
    "preview",
    "require_enabled",
    "service_for",
    "status_out",
]
