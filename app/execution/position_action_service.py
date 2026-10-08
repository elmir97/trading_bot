"""Действия с позицией на бирже (этап 4): карточка и «Да» — биржа и БД.

Расчёт — app/execution/position_actions.py; здесь данные, проверки,
отправка, read-back, журнал. Правила разведки 02.10 (A/B/C) обязательны:
- стоп/тейк: STOP_MARKET / TAKE_PROFIT_MARKET, closePosition=true, quantity =
  текущий объём позиции, stopPrice, MARK_PRICE, без reduceOnly;
- после частичного закрытия стоп/тейк не переставляются;
- перенос стопа/тейка (разведка A, 02.10): второй closePosition-ордер того же
  типа BingX не принимает (110406/110407), поэтому четыре шага — мост с
  quantity на новую цену → отмена старого → closePosition на новую цену →
  отмена моста; каждый ордер подтверждается read-back по openOrders, снятие —
  только повторным openOrders (ответ отмены не доказательство: у
  closePosition-ордера он с type LIMIT и пустым stopPrice). Позиция под
  стопом/тейком на каждом шаге;
- orderId строкой.

Идемпотентность: строка position_actions (снимок карточки) порождает
client_order_id ордеров (action_client_order_id); строка execution_orders в
PENDING коммитится ДО HTTP — UNIQUE на client_order_id не пустит второй
ордер на повторное «Да» или после рестарта. Под Redis-локом позиции
(exec:lock:…) reconciler цикл пропускает.

EXEC_DRY_RUN — на биржу ничего не уходит: строки DRY_RUN с тем, что ушло
бы. Live — только через существующие флаги (exec_allow_live_mode_orders,
bingx_trading_mode), по умолчанию демо.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLock, position_lock_key
from app.core.logging import get_logger
from app.core.numfmt import fmt_amount, fmt_price, fmt_qty
from app.core.security import SecretCipher
from app.database.models.execution_order import ExecutionOrder
from app.database.models.position_action import PositionAction
from app.database.models.trade import Trade
from app.database.models.user import User
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.exchanges.base import (
    ExchangeClient,
    ExchangeError,
    OpenOrder,
    OrderNotFoundError,
    ReadbackIncomplete,
)
from app.execution import guards
from app.execution.models import ExecutionRefusal, action_client_order_id
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.position_actions import (
    ActionInputs,
    ActionPlan,
    levels_after_partial,
    plan_action,
    render_card,
)
from app.execution.position_view import (
    ProtectiveOrder,
    apply_exchange_levels,
    link_trade,
    protective_orders,
)
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.services.permissions import refresh_permissions
from app.services.position_mode import refresh_position_mode
from app.trading.enums import (
    FillSide,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionActionKind,
    PositionActionStatus,
    TradeSide,
)
from app.trading.journal import JournalError, TradeJournal

logger = get_logger(__name__)

ZERO = Decimal(0)
_CLOSING_SIDE = {TradeSide.LONG: OrderSide.SELL, TradeSide.SHORT: OrderSide.BUY}
# Кэш режима позиций — на процесс, как у входа (app/services/position_mode.py).
_position_mode_cache = TTLCache()

EXIT_REASON_PARTIAL = "Частично закрыто через бота"
EXIT_REASON_FULL = "Закрыто через бота"


@dataclass(frozen=True, slots=True)
class CardOutcome:
    text: str
    action: PositionAction | None
    risk_increase: bool = False

    @property
    def refused(self) -> bool:
        return self.action is None or self.action.status is not PositionActionStatus.CARD


@dataclass(frozen=True, slots=True)
class ConfirmOutcome:
    text: str
    final: bool = True   # False — карточку не трогать (лок занят)


class PositionActionService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        cipher: SecretCipher | None,
        user: User,
        *,
        redis: Any = None,
        factory: Any = None,
        market_cache: TTLCache | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._user = user
        self._redis = redis
        self._factory = factory or ExchangeFactory(settings, cipher)  # type: ignore[arg-type]
        self._cache = market_cache or TTLCache()

    # --- проверки и данные -------------------------------------------------

    def _gate(self) -> ExecutionRefusal | None:
        s = self._settings
        return (
            guards.check_execution_enabled(execution_enabled=s.trading_execution_enabled)
            or guards.check_live_orders_allowed(
                trading_mode=s.bingx_trading_mode,
                allow_live_mode_orders=s.exec_allow_live_mode_orders,
            )
            or guards.check_mode_allowed(
                selected_mode=self._user.settings.active_exchange_mode,
                allowed_mode=s.bingx_allowed_exchange_mode,
            )
        )

    async def _client(self) -> tuple[ExchangeClient | None, ExecutionRefusal | None]:
        mode = self._settings.bingx_allowed_exchange_mode
        creds = await self._factory.get_credentials(self._session, self._user.id, mode=mode)
        if creds is None:
            return None, guards.check_trading_key(has_key=False, key_can_trade_futures=False)
        client = await self._factory.for_user(self._session, self._user.id, mode=mode)
        perms = await refresh_permissions(
            self._session, creds, client, ttl_hours=self._settings.exec_permissions_ttl_hours
        )
        refusal = guards.check_permissions_trustworthy(trustworthy=perms.trustworthy) or (
            guards.check_trading_key(has_key=True, key_can_trade_futures=not creds.is_read_only)
        )
        if refusal is None:
            mode_outcome = await refresh_position_mode(
                _position_mode_cache, client, self._user.id,
                ttl_seconds=self._settings.exec_position_mode_ttl_seconds,
            )
            refusal = guards.check_position_mode_known(known=mode_outcome.trustworthy)
            if refusal is None and mode_outcome.dual_side_position is not True:
                refusal = ExecutionRefusal(
                    Code.POSITION_MODE_UNKNOWN,
                    "Счёт в одностороннем режиме — действия работают только в хедж-режиме.",
                )
        if refusal is not None:
            await client.close()
            return None, refusal
        return client, None

    async def _inputs(
        self, client: ExchangeClient, symbol: str, side: TradeSide
    ) -> tuple[ActionInputs | None, Trade | None, ExecutionRefusal | None]:
        positions = await client.get_positions()
        position = next((p for p in positions if p.symbol == symbol and p.side is side), None)
        if position is None:
            return None, None, ExecutionRefusal(
                Code.POSITION_GONE, f"Позиции {symbol} {side.value} на бирже нет."
            )
        orders = await client.get_open_orders(symbol)
        stops, takes = protective_orders(position, orders)
        mark = await client.get_mark_price(symbol)
        info = await MarketDataService(client, self._cache).get_symbol_info(symbol)
        if info is None:
            return None, None, ExecutionRefusal(
                Code.SYMBOL_DATA_UNAVAILABLE, f"Биржа не отдала параметры {symbol}."
            )
        try:
            equity: Decimal | None = (await client.get_balance()).equity
        except ExchangeError:
            logger.warning("Баланс для потолка риска не получен", extra={"user_id": self._user.id})
            equity = None
        plan = await UserRepository(self._session).get_trading_plan(self._user.id)
        trades = await TradeRepository(self._session).list_open_for_reconcile(
            self._user.id, account_mode=self._settings.bingx_allowed_exchange_mode
        )
        trade = link_trade(
            position, trades, account_mode=self._settings.bingx_allowed_exchange_mode
        )
        fee_unit: Decimal | None = None
        one_r: Decimal | None = None
        if trade is not None:
            entries = [f for f in trade.fills if f.fill_side is FillSide.ENTRY]
            entry_qty = sum((f.quantity for f in entries), ZERO)
            if entry_qty > ZERO:
                fee_unit = sum((f.fee for f in entries), ZERO) / entry_qty
            if trade.risk_stop is not None and trade.entry_price is not None:
                one_r = abs(trade.entry_price - trade.risk_stop) or None
        return ActionInputs(
            position=position, stops=stops, takes=takes, mark=mark, symbol_info=info,
            fee_rate=self._settings.exec_taker_fee_rate,
            min_distance_percent=self._settings.exec_min_stop_distance_percent,
            entry_fee_per_unit=fee_unit, one_r_per_unit=one_r, equity=equity,
            risk_cap_percent=plan.risk_per_trade_percent if plan else None,
        ), trade, None

    # --- карточка ----------------------------------------------------------

    async def open_card(
        self,
        kind: PositionActionKind,
        params: dict[str, object],
        symbol: str,
        side: TradeSide,
        *,
        message_id: int | None,
    ) -> CardOutcome:
        refusal = self._gate()
        if refusal is not None:
            return CardOutcome(await self._refused(kind, params, symbol, side, refusal), None)
        client, refusal = await self._client()
        if client is None:
            assert refusal is not None
            return CardOutcome(await self._refused(kind, params, symbol, side, refusal), None)
        try:
            inputs, trade, refusal = await self._inputs(client, symbol, side)
        finally:
            await client.close()
        if inputs is None:
            assert refusal is not None
            return CardOutcome(await self._refused(kind, params, symbol, side, refusal), None)
        plan = plan_action(kind, params, inputs)
        if isinstance(plan, ExecutionRefusal):
            return CardOutcome(
                await self._refused(kind, params, symbol, side, plan, inputs=inputs), None
            )
        action = PositionAction(
            user_id=self._user.id,
            trade_id=trade.id if trade is not None else None,
            symbol=symbol, side=side, position_id=inputs.position.position_id,
            kind=kind, status=PositionActionStatus.CARD, params=plan.params,
            card_message_id=message_id,
            **_snapshot(plan, inputs),
        )
        self._session.add(action)
        await self._session.commit()
        return CardOutcome(render_card(plan, inputs), action, plan.risk_increase)

    async def attach_message(self, action: PositionAction, message_id: int) -> None:
        """Карточка отправлена новым сообщением (после ввода цены): «Да»
        сверяет id сообщения с карточкой."""
        action.card_message_id = message_id
        await self._session.commit()

    async def _refused(
        self, kind: PositionActionKind, params: dict[str, object], symbol: str,
        side: TradeSide, refusal: ExecutionRefusal, *, inputs: ActionInputs | None = None,
    ) -> str:
        """Отказ на карточке — тоже строка (REFUSED с кодом): сводка видит, что
        и почему не дали сделать."""
        action = PositionAction(
            user_id=self._user.id, symbol=symbol, side=side, kind=kind,
            status=PositionActionStatus.REFUSED, params=_jsonable(params),
            error_code=refusal.code.value, error_message=refusal.message,
            position_id=inputs.position.position_id if inputs else None,
            mark_price=inputs.mark if inputs else None,
            position_qty=inputs.position.quantity if inputs else None,
            decided_at=datetime.now(UTC),
        )
        self._session.add(action)
        await self._session.commit()
        return f"⛔ {refusal.message}"

    # --- «Да» / «Нет» ------------------------------------------------------

    async def _load(self, action_id: int) -> PositionAction | None:
        action: PositionAction | None = await self._session.scalar(
            select(PositionAction).where(
                PositionAction.id == action_id, PositionAction.user_id == self._user.id
            )
        )
        return action

    async def decline(self, action_id: int) -> str:
        action = await self._load(action_id)
        if action is None or action.status is not PositionActionStatus.CARD:
            return "Карточка уже обработана."
        action.status = PositionActionStatus.DECLINED
        action.decided_at = datetime.now(UTC)
        await self._session.commit()
        return "Отменено — на биржу ничего не отправлено."

    async def confirm(
        self, action_id: int, *, message_id: int | None, risk_confirmed: bool
    ) -> ConfirmOutcome:
        action = await self._load(action_id)
        if action is None or action.status is not PositionActionStatus.CARD:
            return ConfirmOutcome("Карточка уже обработана — открой действие заново.")
        if action.card_message_id is not None and message_id != action.card_message_id:
            return ConfirmOutcome("Это не последняя карточка — открой действие заново.")
        now = datetime.now(UTC)
        if now - action.created_at > timedelta(seconds=self._settings.exec_confirm_ttl_seconds):
            return ConfirmOutcome(
                await self._finish(action, PositionActionStatus.EXPIRED, ExecutionRefusal(
                    Code.CARD_EXPIRED, "Карточка устарела — цены могли уйти. Открой заново."
                ))
            )
        if action.risk_increase and not risk_confirmed:
            return ConfirmOutcome(
                await self._finish(action, PositionActionStatus.REFUSED, ExecutionRefusal(
                    Code.RISK_INCREASE_NOT_CONFIRMED,
                    "Риск растёт — нужна кнопка «⚠️ Да, увеличить риск».",
                ))
            )
        if self._redis is None:
            return ConfirmOutcome("Подтверждение недоступно: нет Redis для лока.", final=False)
        key = position_lock_key(self._user.id, action.symbol, action.side.value)
        try:
            async with RedisLock(self._redis, key, self._settings.confirm_lock_ttl_seconds):
                return ConfirmOutcome(await self._confirm_locked(action))
        except LockBusyError:
            return ConfirmOutcome("Действие с этой позицией уже выполняется.", final=False)

    async def _confirm_locked(self, action: PositionAction) -> str:
        refusal = self._gate()
        if refusal is not None:
            return await self._finish(action, PositionActionStatus.REFUSED, refusal)
        client, refusal = await self._client()
        if client is None:
            assert refusal is not None
            return await self._finish(action, PositionActionStatus.REFUSED, refusal)
        try:
            try:
                inputs, trade, refusal = await self._inputs(client, action.symbol, action.side)
            except ExchangeError as exc:
                return await self._finish(action, PositionActionStatus.REFUSED, ExecutionRefusal(
                    Code.POSITION_CHANGED, f"Биржа не ответила перед отправкой: {exc}"
                ))
            if inputs is None:
                assert refusal is not None
                return await self._finish(action, PositionActionStatus.REFUSED, refusal)
            changed = _drift(action, inputs)
            if changed is not None:
                return await self._finish(action, PositionActionStatus.REFUSED, changed)
            plan = plan_action(action.kind, dict(action.params or {}), inputs)
            if isinstance(plan, ExecutionRefusal):
                return await self._finish(action, PositionActionStatus.REFUSED, plan)
            if plan.risk_increase != action.risk_increase or plan.new_level != action.new_level:
                return await self._finish(action, PositionActionStatus.REFUSED, ExecutionRefusal(
                    Code.POSITION_CHANGED, "Цифры карточки изменились — открой действие заново."
                ))
            if self._settings.exec_dry_run:
                return await self._dry_run(action, plan, inputs)
            if action.kind in (PositionActionKind.MOVE_STOP, PositionActionKind.SET_TAKE):
                return await self._move_level(client, action, plan, inputs, trade)
            return await self._close(client, action, plan, inputs, trade)
        finally:
            await client.close()

    async def _finish(
        self, action: PositionAction, status: PositionActionStatus,
        refusal: ExecutionRefusal | None = None, text: str | None = None,
    ) -> str:
        action.status = status
        action.decided_at = datetime.now(UTC)
        if refusal is not None:
            action.error_code = refusal.code.value
            action.error_message = refusal.message
        await self._session.commit()
        return text if text is not None else f"⛔ {refusal.message if refusal else status.value}"

    # --- сухой прогон ------------------------------------------------------

    async def _dry_run(self, action: PositionAction, plan: ActionPlan, inputs: ActionInputs) -> str:
        p = inputs.position
        pp = inputs.symbol_info.price_precision
        qp = inputs.symbol_info.quantity_precision
        closing = _CLOSING_SIDE[p.side]
        if action.kind in (PositionActionKind.MOVE_STOP, PositionActionKind.SET_TAKE):
            is_stop = action.kind is PositionActionKind.MOVE_STOP
            role = OrderRole.STOP_LOSS if is_stop else OrderRole.TAKE_PROFIT
            otype = OrderType.STOP_MARKET if is_stop else OrderType.TAKE_PROFIT_MARKET
            row = self._row(action, role, otype, OrderStatus.DRY_RUN, quantity=p.quantity,
                            trigger=plan.new_level)
            what = (
                f"{otype.value} {closing.value}/{p.side.value} closePosition, quantity "
                f"{fmt_qty(p.quantity, qp)}, stopPrice {fmt_price(plan.new_level, pp)} (MARK_PRICE)"
            )
            if plan.replaces_order_id:
                what = (
                    f"мост {otype.value} на {fmt_qty(p.quantity, qp)} по "
                    f"{fmt_price(plan.new_level, pp)} → отмена ордера {plan.replaces_order_id} → "
                    f"{what} → отмена моста"
                )
        else:
            row = self._row(action, OrderRole.CLOSE, OrderType.MARKET, OrderStatus.DRY_RUN,
                            quantity=plan.close_qty, price=inputs.mark)
            what = f"MARKET {closing.value}/{p.side.value} {fmt_qty(plan.close_qty, qp)}"
        self._session.add(row)
        return await self._finish(
            action, PositionActionStatus.DRY_RUN,
            text=f"🧪 Сухой прогон (EXEC_DRY_RUN): на биржу ничего не ушло.\nУшло бы: {what}.",
        )

    # --- отправка: стоп / тейк ----------------------------------------------

    def _row(
        self, action: PositionAction, role: OrderRole, order_type: OrderType,
        status: OrderStatus, *, quantity: Decimal | None, trigger: Decimal | None = None,
        price: Decimal | None = None, client_order_id: str | None = None,
        exchange_order_id: str | None = None,
    ) -> ExecutionOrder:
        return ExecutionOrder(
            user_id=self._user.id, position_action_id=action.id, trade_id=action.trade_id,
            client_order_id=client_order_id, exchange_order_id=exchange_order_id,
            symbol=action.symbol, side=_CLOSING_SIDE[action.side], position_side=action.side,
            order_type=order_type, role=role, quantity=quantity, trigger_price=trigger,
            price=price, status=status,
        )

    async def _insert_pending(self, row: ExecutionOrder) -> bool:
        """PENDING до HTTP. UNIQUE на client_order_id: строка уже есть — ордер
        по этой карточке уже отправлялся, второй не шлём."""
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return False
        return True

    async def _delay(self) -> None:
        await asyncio.sleep(self._settings.exec_order_readback_delay_ms / 1000)

    async def _find_open(
        self, client: ExchangeClient, symbol: str, *, client_order_id: str | None = None,
        order_id: str | None = None,
    ) -> OpenOrder | None:
        orders = await client.get_open_orders(symbol)
        for o in orders:
            if order_id is not None and str(o.order_id) == order_id:
                return o
            if client_order_id and o.client_order_id.casefold() == client_order_id.casefold():
                return o
        return None

    async def _place_and_confirm(
        self, client: ExchangeClient, action: PositionAction, role: OrderRole,
        otype: OrderType, level: Decimal, quantity: Decimal, *, bridge: bool,
    ) -> tuple[OpenOrder | None, ExecutionOrder | None, str]:
        """PENDING (UNIQUE client_order_id) → POST → read-back по openOrders.
        (ордер, строка, "") — встал и подтверждён; (None, строка|None,
        причина) — нет."""
        cid = action_client_order_id(
            action_id=action.id, user_id=self._user.id, role=role, bridge=bridge
        )
        row = self._row(action, role, otype, OrderStatus.PENDING, quantity=quantity,
                        trigger=level, client_order_id=cid)
        if not await self._insert_pending(row):
            return None, None, "по этой карточке уже отправлялся — повторно не шлю"
        try:
            placed = await client.place_conditional_order(
                symbol=action.symbol, side=_CLOSING_SIDE[action.side],
                position_side=action.side.value, order_type=otype.value, stop_price=level,
                quantity=quantity, client_order_id=cid, close_position=not bridge,
            )
        except ExchangeError as exc:
            row.status = OrderStatus.REJECTED if exc.code else OrderStatus.UNKNOWN
            row.error_code = str(exc.code) if exc.code else type(exc).__name__
            row.raw_response = exc.payload
            await self._session.commit()
            if exc.code:
                return None, row, f"биржа не приняла: {exc}"
            return None, row, f"исход отправки неизвестен ({exc}) — проверь ордера в BingX"
        row.status = OrderStatus.SUBMITTED
        row.exchange_order_id = str(placed.order_id) or None
        await self._session.commit()

        found = None
        for attempt in range(self._settings.exec_order_readback_attempts):
            found = await self._find_open(client, action.symbol, client_order_id=cid,
                                          order_id=row.exchange_order_id)
            if found is not None:
                break
            if attempt + 1 < self._settings.exec_order_readback_attempts:
                await self._delay()
        if found is None or found.stop_price != level:
            return None, row, "не подтверждён в openOrders — проверь ордера в BingX"
        row.exchange_order_id = str(found.order_id)
        await self._session.commit()
        return found, row, ""

    def _journal_level(
        self, trade: Trade | None, is_stop: bool, order: OpenOrder, level: Decimal,
        *, whole: bool,
    ) -> None:
        if trade is None:
            return
        new = (ProtectiveOrder(
            str(order.order_id), level, whole, None if whole else order.quantity, "MARK_PRICE"
        ),)
        apply_exchange_levels(trade, new if is_stop else (), () if is_stop else new)

    async def _move_level(
        self, client: ExchangeClient, action: PositionAction, plan: ActionPlan,
        inputs: ActionInputs, trade: Trade | None,
    ) -> str:
        """Стоп/тейк на новую цену. Нет старого — один closePosition-ордер.
        Есть — четыре шага (мост с quantity → отмена старого → closePosition →
        отмена моста), позиция под стопом/тейком на каждом шаге."""
        p = inputs.position
        pp = inputs.symbol_info.price_precision
        qp = inputs.symbol_info.quantity_precision
        is_stop = action.kind is PositionActionKind.MOVE_STOP
        name = "стоп" if is_stop else "тейк"
        two = "два стопа" if is_stop else "два тейка"
        role = OrderRole.STOP_LOSS if is_stop else OrderRole.TAKE_PROFIT
        otype = OrderType.STOP_MARKET if is_stop else OrderType.TAKE_PROFIT_MARKET
        assert plan.new_level is not None
        level = plan.new_level
        new_s = fmt_price(level, pp)
        old_level = plan.current_stop if is_stop else plan.current_take
        old_s = fmt_price(old_level, pp)

        async def failed(message: str, text: str) -> str:
            return await self._finish(
                action, PositionActionStatus.FAILED,
                ExecutionRefusal(Code.POSITION_CHANGED, message), text=text,
            )

        if not plan.replaces_order_id:
            final, _, why = await self._place_and_confirm(
                client, action, role, otype, level, p.quantity, bridge=False
            )
            if final is None:
                return await failed(f"Новый {name}: {why}", f"❌ {name.capitalize()} не "
                                    f"поставлен: {why}.")
            self._journal_level(trade, is_stop, final, level, whole=True)
            await self._session.commit()
            return await self._finish(
                action, PositionActionStatus.DONE,
                text=f"✅ {name.capitalize()} {new_s} (на всю позицию).",
            )

        # Шаг 1: мост — ордер с quantity на новую цену рядом со старым.
        bridge, bridge_row, why = await self._place_and_confirm(
            client, action, role, otype, level, p.quantity, bridge=True
        )
        if bridge is None:
            return await failed(
                f"Промежуточный {name}: {why}",
                f"❌ {name.capitalize()} не перенесён: промежуточный {name} {new_s} — {why}. "
                f"Старый {name} {old_s} на месте.",
            )
        # Шаг 2: снять старый; не снялся — снять мост, вернуть как было.
        if not await self._cancel_and_confirm(
            client, action, plan.replaces_order_id, role, otype, old_level, p.quantity
        ):
            back = await self._cancel_and_confirm(
                client, action, str(bridge.order_id), role, otype, level, p.quantity,
                placed_row=bridge_row,
            )
            tail = (
                f"Промежуточный {new_s} снят — всё как было."
                if back else
                f"Промежуточный {new_s} тоже не снялся — на позиции {two}. Проверь BingX."
            )
            return await failed(
                f"Старый {name} не снялся.",
                f"❌ {name.capitalize()} не перенесён: старый {name} {old_s} не снялся. {tail}",
            )
        # Шаг 3: closePosition на новую цену; не встал — позицию держит мост.
        final, _, why = await self._place_and_confirm(
            client, action, role, otype, level, p.quantity, bridge=False
        )
        if final is None:
            self._journal_level(trade, is_stop, bridge, level, whole=False)
            await self._session.commit()
            return await failed(
                f"{name.capitalize()} на всю позицию: {why}",
                f"⚠️ {name.capitalize()} {old_s} → {new_s} стоит промежуточным ордером на "
                f"{fmt_qty(p.quantity, qp)} (не «на всю позицию»): {why}. Проверь BingX.",
            )
        self._journal_level(trade, is_stop, final, level, whole=True)
        await self._session.commit()
        head = f"✅ {name.capitalize()} {old_s} → {new_s} (на всю позицию)."
        # Шаг 4: снять мост.
        if not await self._cancel_and_confirm(
            client, action, str(bridge.order_id), role, otype, level, p.quantity,
            placed_row=bridge_row,
        ):
            return await failed(
                f"Промежуточный {name} не снялся.",
                f"{head}\n⚠️ Промежуточный {name} {bridge.order_id} на {new_s} не снялся — "
                f"на позиции {two} на одной цене. Сними его в BingX.",
            )
        return await self._finish(
            action, PositionActionStatus.DONE,
            text=f"{head}\nСтарый {name} и промежуточный сняты — проверено по openOrders.",
        )

    async def _cancel_and_confirm(
        self, client: ExchangeClient, action: PositionAction, order_id: str, role: OrderRole,
        otype: OrderType, level: Decimal | None, quantity: Decimal | None,
        *, placed_row: ExecutionOrder | None = None,
    ) -> bool:
        """Отмена и подтверждение только повторным openOrders. Строка-наблюдение
        в execution_orders: CANCELLED — ордера в openOrders больше нет.

        placed_row — строка постановки нашего ордера (мост переноса, этап 5):
        при подтверждённом снятии она сама становится CANCELLED, без строки-
        наблюдения; не снялся — остаётся SUBMITTED, наблюдение UNKNOWN."""
        try:
            await client.cancel_order(action.symbol, order_id)
        except OrderNotFoundError:
            pass  # уже нет — подтвердит openOrders
        except ExchangeError:
            logger.warning("Отмена ордера не прошла", extra={"order_id": order_id})
        gone = False
        for attempt in range(self._settings.exec_order_readback_attempts):
            if await self._find_open(client, action.symbol, order_id=order_id) is None:
                gone = True
                break
            if attempt + 1 < self._settings.exec_order_readback_attempts:
                await self._delay()
        if gone and placed_row is not None:
            placed_row.status = OrderStatus.CANCELLED
        else:
            self._session.add(self._row(
                action, role, otype, OrderStatus.CANCELLED if gone else OrderStatus.UNKNOWN,
                quantity=quantity, trigger=level, exchange_order_id=order_id,
            ))
        await self._session.commit()
        return gone

    # --- отправка: закрытие ---------------------------------------------------

    async def _close(
        self, client: ExchangeClient, action: PositionAction, plan: ActionPlan,
        inputs: ActionInputs, trade: Trade | None,
    ) -> str:
        p = inputs.position
        pp = inputs.symbol_info.price_precision
        qp = inputs.symbol_info.quantity_precision
        assert plan.close_qty is not None
        cid = action_client_order_id(action_id=action.id, user_id=self._user.id,
                                     role=OrderRole.CLOSE)
        row = self._row(action, OrderRole.CLOSE, OrderType.MARKET, OrderStatus.PENDING,
                        quantity=plan.close_qty, price=inputs.mark, client_order_id=cid)
        if not await self._insert_pending(row):
            return await self._finish(action, PositionActionStatus.FAILED, ExecutionRefusal(
                Code.CARD_STALE, "Закрытие по этой карточке уже отправлялось — повторно не шлю."
            ))
        try:
            placed = await client.place_market_order(
                symbol=action.symbol, side=_CLOSING_SIDE[p.side], position_side=p.side.value,
                quantity=plan.close_qty, client_order_id=cid,
            )
        except ExchangeError as exc:
            row.status = OrderStatus.REJECTED if exc.code else OrderStatus.UNKNOWN
            row.error_code = str(exc.code) if exc.code else type(exc).__name__
            row.raw_response = exc.payload
            unknown = row.status is OrderStatus.UNKNOWN
            return await self._finish(
                action, PositionActionStatus.FAILED,
                ExecutionRefusal(Code.POSITION_CHANGED, f"Закрытие не прошло: {exc}"),
                text=(
                    "⚠️ Исход отправки неизвестен — проверь позицию в BingX. Повторно не отправляю."
                    if unknown else f"❌ Биржа не приняла закрытие: {exc}"
                ),
            )
        row.status = OrderStatus.SUBMITTED
        row.exchange_order_id = str(placed.order_id) or None
        await self._session.commit()

        fill = None
        for attempt in range(self._settings.exec_order_readback_attempts):
            try:
                fill = await client.get_order_fill(action.symbol, cid, max_retries=1)
            except (ReadbackIncomplete, ExchangeError):
                fill = None
            if fill is not None and fill.status == "FILLED" and fill.executed_qty > ZERO:
                break
            fill = None
            if attempt + 1 < self._settings.exec_order_readback_attempts:
                await self._delay()
        if fill is None:
            row.status = OrderStatus.UNKNOWN
            return await self._finish(
                action, PositionActionStatus.FAILED,
                ExecutionRefusal(Code.POSITION_CHANGED, "Исполнение закрытия не подтверждено."),
                text=(
                    "⚠️ Исполнение не подтверждено — проверь позицию в BingX. "
                    "Повторно не отправляю."
                ),
            )
        row.status = OrderStatus.FILLED
        row.exchange_order_id = str(fill.order_id)
        row.price = fill.avg_price
        row.quantity = fill.executed_qty
        await self._session.commit()

        full = action.kind is PositionActionKind.CLOSE_FULL
        if trade is not None:
            await self._journal_exit(trade.id, fill, full)
        text = (
            f"✅ Закрыто {fmt_qty(fill.executed_qty, qp)} по {fmt_price(fill.avg_price, pp)} · "
            f"комиссия {fmt_amount(fill.fee)} USDT."
        )
        if not full:
            rest = p.quantity - fill.executed_qty
            text += "\n" + levels_after_partial(inputs.stops, inputs.takes, rest, qp) + "."
            return await self._finish(action, PositionActionStatus.DONE, text=text)
        leftovers = [
            o for o in await client.get_open_orders(action.symbol)
            if o.position_side == p.side.value and o.side == _CLOSING_SIDE[p.side].value
            and o.stop_price is not None
        ]
        for o in leftovers:
            await self._cancel_and_confirm(
                client, action, str(o.order_id), OrderRole.STOP_LOSS, OrderType.STOP_MARKET,
                o.stop_price, o.quantity,
            )
        if leftovers:
            ids = {str(o.order_id) for o in leftovers}
            still = await self._find_any(client, action.symbol, ids)
            if still:
                text += f"\n⚠️ Не сняты условные ордера: {', '.join(sorted(still))}."
                return await self._finish(action, PositionActionStatus.DONE, text=text)
            text += "\nОставшиеся условные ордера сняты — проверено по openOrders."
        else:
            text += "\nСтоп и тейк биржа сняла сама."
        return await self._finish(action, PositionActionStatus.DONE, text=text)

    async def _find_any(self, client: ExchangeClient, symbol: str, ids: set[str]) -> set[str]:
        return {str(o.order_id) for o in await client.get_open_orders(symbol)} & ids

    async def _journal_exit(self, trade_id: int, fill: Any, full: bool) -> None:
        """Выход в журнал сразу из read-back: external_fill_id = orderId —
        reconciler его не запишет второй раз (recorded_order_ids / exits_after)."""
        trades = TradeRepository(self._session)
        trade = await trades.lock_for_reconcile(trade_id)
        if trade is None or not trade.is_open:
            return
        try:
            async with self._session.begin_nested():
                await TradeJournal(trades).close_trade(
                    trade, exit_price=fill.avg_price,
                    quantity=None if full else fill.executed_qty, fee=fill.fee,
                    exit_reason=EXIT_REASON_FULL if full else EXIT_REASON_PARTIAL,
                    closed_at=fill.filled_at or datetime.now(UTC),
                    external_fill_id=str(fill.order_id),
                )
        except (IntegrityError, JournalError):
            logger.warning("Выход не записан в журнал — допишет сверка", exc_info=True,
                           extra={"trade_id": trade_id})
        await self._session.commit()


def _snapshot(plan: ActionPlan, inputs: ActionInputs) -> dict[str, Any]:
    return {
        "mark_price": inputs.mark,
        "entry_price": inputs.position.entry_price,
        "position_qty": inputs.position.quantity,
        "close_qty": plan.close_qty,
        "current_stop": plan.current_stop,
        "current_take": plan.current_take,
        "new_level": plan.new_level,
        "risk_before": plan.risk_before,
        "risk_after": plan.risk_after,
        "risk_before_r": plan.r_before,
        "risk_after_r": plan.r_after,
        "fee": plan.fee,
        "risk_increase": plan.risk_increase,
    }


def _jsonable(params: dict[str, object]) -> dict[str, object]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in params.items()}


def _drift(action: PositionAction, inputs: ActionInputs) -> ExecutionRefusal | None:
    """Позиция, стоп и тейк на «Да» — те же, что на карточке."""
    p = inputs.position
    if action.position_id and p.position_id and action.position_id != p.position_id:
        return ExecutionRefusal(Code.POSITION_CHANGED, "Это уже другая позиция — открой заново.")
    if action.position_qty is not None and p.quantity != action.position_qty:
        return ExecutionRefusal(
            Code.POSITION_CHANGED,
            f"Объём позиции изменился: {action.position_qty.normalize()} → "
            f"{p.quantity.normalize()} — открой заново.",
        )
    stop = inputs.stops[0].trigger_price if len(inputs.stops) == 1 else None
    take = inputs.takes[0].trigger_price if len(inputs.takes) == 1 else None
    if stop != action.current_stop or take != action.current_take:
        return ExecutionRefusal(
            Code.POSITION_CHANGED, "Стоп или тейк на бирже изменились — открой заново."
        )
    return None
