"""Открытие сделки из бота — сервис (docs/open-trade-plan.md, §2, §5).

Без aiogram: вызывают чат-мастер и Mini App. Здесь — данные с биржи, гварды,
расчёт карточки (calc + checks), снимок в trade_openings. Исполнение
(«Открыть») — confirm(), восстановление — recovery.

Гварды — те же, что у действий этапа 4: исполнение включено, режим счёта
настроек совпадает с разрешённым конфигом, ключ с правом торговли, хедж-режим.
Плюс свой: открытие на LIVE — только с EXEC_OPEN_ALLOW_LIVE.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLock, close_wanted_key, position_lock_key
from app.core.logging import get_logger
from app.core.numfmt import fmt_price, fmt_qty
from app.core.security import SecretCipher
from app.database.models.execution_order import ExecutionOrder
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.exchanges.base import ExchangeClient, ExchangeError, MarginType, SymbolInfo
from app.execution import guards
from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.opening import checks, limits
from app.execution.opening.calc import (
    Issue,
    Level,
    Limits,
    MarketSnapshot,
    OpeningCalc,
    OpeningInputs,
    compute,
)
from app.execution.opening.execution import (
    CLOSING_SIDE,
    EXIT_REASON_MANUAL,
    OPENING_SIDE,
    Runner,
    opening_client_order_id,
    transition,
)
from app.execution.opening.flow import FlowOutcome, advance, after_protect, working_text
from app.execution.opening.render import render_card, render_refusal
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.services.permissions import refresh_permissions
from app.services.position_mode import refresh_position_mode
from app.trading.enums import (
    EntryType,
    ExchangeKeyMode,
    OpeningSource,
    OpeningStatus,
    OrderRole,
    OrderStatus,
    OrderType,
)
from app.trading.risk import PlanValidator, tz_offset_for

logger = get_logger(__name__)

# На процесс, как у действий этапа 4: режим позиций и ставка комиссии меняются
# редко, лимит marginType — 2 запроса в секунду.
_position_mode_cache = TTLCache()
_commission_cache = TTLCache()
COMMISSION_TTL_SECONDS = 3600
LIMIT_EXPIRY_CHOICES = (15, 60, 240, 1440)


def desired_margin_type(user: User) -> MarginType:
    raw = user.settings.margin_type_default if user.settings else None
    return MarginType(raw) if raw in tuple(MarginType) else MarginType.ISOLATED


@dataclass(frozen=True, slots=True)
class CardOutcome:
    """Карточка или отказ. opening — строка trade_openings (None у preview)."""

    text: str
    opening: TradeOpening | None
    calc: OpeningCalc | None
    issues: tuple[Issue, ...]
    refusal: ExecutionRefusal | None = None
    market: MarketSnapshot | None = None

    @property
    def can_open(self) -> bool:
        return self.refusal is None and self.calc is not None and not any(
            i.level is Level.BLOCK for i in self.issues
        )

    @property
    def has_warnings(self) -> bool:
        return any(i.level is Level.WARN for i in self.issues)


@dataclass(frozen=True, slots=True)
class ConfirmOutcome:
    """Итог «Открыть». final=False — карточку не трогать (лок занят, нужно
    «Открыть всё равно»): кнопки остаются."""

    text: str
    status: OpeningStatus | None = None
    trade_id: int | None = None
    final: bool = True
    # Лок позиции занят — ответ всплывашкой на нажатие, не сообщением
    # (деплой 3, фикс 6).
    busy: bool = False


# Вызывается сразу после захвата лока позиции: «Открываю…»/«Закрываю…» на
# нажатие — только когда действие точно пошло (иначе ответ — всплывашка «Уже
# идёт действие»: на одно нажатие Telegram принимает один ответ).
OnLocked = Callable[[], Awaitable[None]]
BUSY_TEXT = "Уже идёт действие с этой позицией — подожди пару секунд."


def _alarm_gone(opening: TradeOpening | None) -> ConfirmOutcome:
    """Тревоги уже нет — закрывать по кнопке не нужно (итог — по факту)."""
    if opening is None:
        return ConfirmOutcome("Открытие не найдено.", final=False)
    texts = {
        OpeningStatus.DONE: f"Стоп уже стоит — позиция под защитой (сделка #{opening.trade_id}). "
                            "Закрыть можно в «Позиции».",
        OpeningStatus.EMERGENCY_CLOSED: (
            f"Позиция уже закрыта (сделка #{opening.trade_id})." if opening.trade_id
            else "Позиция уже закрыта — сделка запишется следом."
        ),
    }
    return ConfirmOutcome(
        texts.get(opening.status, STATUS_TEXT.get(opening.status, "Тревоги уже нет.")),
        opening.status, opening.trade_id, final=False,
    )


def inputs_of(opening: TradeOpening) -> OpeningInputs:
    return OpeningInputs(
        symbol=opening.symbol, side=opening.side, entry_type=opening.entry_type,
        stop_loss=opening.stop_loss, risk_percent=opening.risk_percent,
        leverage=opening.leverage, limit_price=opening.limit_price,
        take_profit=opening.take_profit, expiry_minutes=opening.expiry_minutes,
    )


STATUS_TEXT = {
    OpeningStatus.DECLINED: "Открытие отменено.",
    OpeningStatus.EXPIRED_CARD: "Карточка устарела — пересчитай.",
    OpeningStatus.REFUSED: "Открытие отклонено проверкой.",
    OpeningStatus.DRY_RUN: "Сухой прогон уже выполнен.",
    OpeningStatus.CONFIRMED: "Уже открываю…",
    OpeningStatus.SUBMITTING: "Уже открываю…",
    OpeningStatus.UNKNOWN: "Проверяю, открылась ли позиция…",
    OpeningStatus.WORKING: "Лимит уже выставлен.",
    OpeningStatus.FILLED: "Вход исполнен, ставлю защиту…",
    OpeningStatus.PROTECTED: "Позиция под стопом, записываю сделку…",
    OpeningStatus.DONE: "Сделка уже открыта.",
    OpeningStatus.REJECTED: "Биржа отклонила этот вход.",
    OpeningStatus.NOT_PLACED: "Вход не выставлен на бирже.",
    OpeningStatus.CANCELLED: "Лимит отменён.",
    OpeningStatus.EXPIRED: "Лимит истёк.",
    OpeningStatus.EMERGENCY_CLOSED: "Позиция закрыта аварийно — стоп не встал.",
    OpeningStatus.ALARM: "🚨 Позиция без стопа — закрой вручную.",
}


class OpeningService:
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

    @property
    def account_mode(self) -> ExchangeKeyMode:
        return self._settings.bingx_allowed_exchange_mode

    # --- гварды и клиент -----------------------------------------------------

    def _gate(self) -> ExecutionRefusal | None:
        s = self._settings
        refusal = (
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
        if refusal is None and self.account_mode is ExchangeKeyMode.LIVE and s.exec_open_faults:
            # Вторая защита (первая — проверка настроек на старте): управляемый
            # сбой — только демо.
            refusal = ExecutionRefusal(
                Code.LIVE_ORDERS_NOT_ALLOWED,
                "Включён управляемый сбой EXEC_OPEN_FAULT — открытие на реальном счёте запрещено.",
            )
        if refusal is None and self.account_mode is ExchangeKeyMode.LIVE and not (
            s.exec_open_allow_live
        ):
            refusal = ExecutionRefusal(
                Code.LIVE_ORDERS_NOT_ALLOWED,
                "Открытие сделок на реальном счёте пока выключено (EXEC_OPEN_ALLOW_LIVE).",
            )
        return refusal

    async def client(self) -> tuple[ExchangeClient | None, ExecutionRefusal | None]:
        mode = self.account_mode
        creds = await self._factory.get_credentials(self._session, self._user.id, mode=mode)
        if creds is None:
            return None, guards.check_trading_key(has_key=False, key_can_trade_futures=False)
        client = await self._factory.for_user(self._session, self._user.id, mode=mode)
        try:
            perms = await refresh_permissions(
                self._session, creds, client, ttl_hours=self._settings.exec_permissions_ttl_hours
            )
            refusal = guards.check_permissions_trustworthy(trustworthy=perms.trustworthy) or (
                guards.check_trading_key(
                    has_key=True, key_can_trade_futures=not creds.is_read_only
                )
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
                        "Счёт в одностороннем режиме — открытие работает только в хедж-режиме.",
                    )
        except BaseException:
            await client.close()
            raise
        if refusal is not None:
            await client.close()
            return None, refusal
        return client, None

    async def market_snapshot(
        self, client: ExchangeClient, inputs: OpeningInputs
    ) -> tuple[MarketSnapshot | None, ExecutionRefusal | None]:
        symbol = inputs.symbol
        try:
            info = await MarketDataService(client, self._cache).get_symbol_info(symbol)
            if info is None:
                return None, ExecutionRefusal(
                    Code.SYMBOL_DATA_UNAVAILABLE, f"Контракта {symbol} на BingX нет."
                )
            ticker = await client.get_ticker(symbol)
            balance = await client.get_balance()
            leverage = await client.get_leverage(symbol)
            margin_type = await client.get_margin_type(symbol)
            positions = await client.get_positions()
            orders = await client.get_open_orders(symbol)
            rate = await _commission_cache.get_or_fetch(
                f"commission:{self._user.id}:{self.account_mode.value}",
                COMMISSION_TTL_SECONDS, client.get_commission_rate,
            )
        except ExchangeError as exc:
            logger.warning(
                "Карточка открытия: биржа не ответила",
                extra={"user_id": self._user.id, "symbol": symbol, "error": type(exc).__name__},
            )
            return None, ExecutionRefusal(
                Code.SYMBOL_DATA_UNAVAILABLE, f"Биржа не ответила ({type(exc).__name__}) — повтори."
            )
        position = next(
            (p for p in positions if p.symbol == symbol and p.side is inputs.side), None
        )
        busy = bool(orders) or any(p.symbol == symbol for p in positions)
        side_max = (
            leverage.max_long_leverage if inputs.side.direction > 0
            else leverage.max_short_leverage
        )
        return MarketSnapshot(
            last_price=ticker.last_price, symbol_info=info, equity=balance.equity,
            available=balance.available, taker_rate=rate.taker, max_leverage=side_max,
            margin_type=margin_type, desired_margin_type=desired_margin_type(self._user),
            position=position, symbol_busy=busy,
        ), None

    # --- карточка -------------------------------------------------------------

    def limits(self, plan: Any) -> Limits:
        s = self._settings
        return Limits(
            plan_risk_percent=plan.risk_per_trade_percent if plan else None,
            plan_max_leverage=plan.max_leverage if plan else None,
            min_stop_distance_percent=s.exec_min_stop_distance_percent,
            liq_buffer=s.exec_open_liq_buffer,
            mmr=s.exec_open_mmr,
        )

    async def evaluate(
        self, inputs: OpeningInputs, market: MarketSnapshot
    ) -> tuple[OpeningCalc, tuple[Issue, ...]]:
        plan = await UserRepository(self._session).get_trading_plan(self._user.id)
        calc = compute(inputs, market, self.limits(plan))
        plan_issues: list[Issue] = []
        if plan is not None and calc.quantity > 0:
            check = await PlanValidator(
                TradeRepository(self._session),
                tz_offset_for(self._user.settings.timezone if self._user.settings else None),
            ).check(
                plan=plan, user_id=self._user.id, symbol=inputs.symbol, side=inputs.side,
                entry_price=calc.entry_price, quantity=calc.quantity,
                stop_loss=inputs.stop_loss, take_profit=inputs.take_profit,
                leverage=inputs.leverage, timeframe=None, account_balance=market.equity,
                account_mode=self.account_mode,
            )
            plan_issues = checks.classify(check)
        return calc, checks.merge(calc.issues, plan_issues)

    async def preview(self, inputs: OpeningInputs) -> CardOutcome:
        """Карточка без записи (Mini App: живой пересчёт)."""
        return await self._card(inputs, persist=False, source=OpeningSource.MINIAPP)

    async def prepare(
        self,
        inputs: OpeningInputs,
        *,
        source: OpeningSource,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> CardOutcome:
        """Карточка со снимком в trade_openings: CARD (можно открыть) или
        REFUSED (отказ гварда, данных или блокирующее нарушение)."""
        return await self._card(
            inputs, persist=True, source=source, chat_id=chat_id, message_id=message_id
        )

    async def _card(
        self,
        inputs: OpeningInputs,
        *,
        persist: bool,
        source: OpeningSource,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> CardOutcome:
        refusal = self._gate()
        market: MarketSnapshot | None = None
        if refusal is None:
            client, refusal = await self.client()
            if client is not None:
                try:
                    market, refusal = await self.market_snapshot(client, inputs)
                finally:
                    await client.close()
        if refusal is not None or market is None:
            assert refusal is not None
            opening = (
                await self._store(inputs, source, None, None, (), refusal, chat_id, message_id)
                if persist else None
            )
            return CardOutcome(render_refusal(refusal.message), opening, None, (), refusal)

        calc, issues = await self.evaluate(inputs, market)
        info = market.symbol_info
        calc_view = _with_issues(calc, issues)
        text = render_card(
            inputs, calc_view, account_mode=self.account_mode, equity=market.equity,
            available=market.available, margin_type=market.desired_margin_type,
            price_precision=info.price_precision, quantity_precision=info.quantity_precision,
            dry_run=self._settings.exec_open_dry_run,
            ttl_seconds=self._settings.exec_open_card_ttl_seconds,
        )
        opening = (
            await self._store(inputs, source, market, calc, issues, None, chat_id, message_id)
            if persist else None
        )
        return CardOutcome(text, opening, calc_view, issues, None, market)

    async def _store(
        self,
        inputs: OpeningInputs,
        source: OpeningSource,
        market: MarketSnapshot | None,
        calc: OpeningCalc | None,
        issues: tuple[Issue, ...],
        refusal: ExecutionRefusal | None,
        chat_id: int | None,
        message_id: int | None,
    ) -> TradeOpening:
        blocks = [i for i in issues if i.level is Level.BLOCK]
        status = (
            OpeningStatus.REFUSED if refusal is not None or blocks else OpeningStatus.CARD
        )
        opening = TradeOpening(
            user_id=self._user.id,
            source=source,
            account_mode=self.account_mode,
            status=status,
            symbol=inputs.symbol,
            side=inputs.side,
            entry_type=inputs.entry_type,
            limit_price=inputs.limit_price if inputs.entry_type is EntryType.LIMIT else None,
            stop_loss=inputs.stop_loss,
            take_profit=inputs.take_profit,
            risk_percent=inputs.risk_percent,
            leverage=inputs.leverage,
            margin_type=(
                market.desired_margin_type if market else desired_margin_type(self._user)
            ).value,
            expiry_minutes=inputs.expiry_minutes if inputs.entry_type is EntryType.LIMIT else None,
            card_price=calc.entry_price if calc else None,
            equity=market.equity if market else None,
            available=market.available if market else None,
            quantity=calc.quantity if calc else None,
            risk_usd=calc.risk_usd if calc else None,
            fee_estimate=calc.fee_usd if calc else None,
            margin=calc.margin if calc else None,
            rr=calc.rr if calc else None,
            liq_estimate=calc.liq_estimate if calc else None,
            violations=[i.as_json() for i in issues] or None,
            chat_id=chat_id,
            card_message_id=message_id,
            error_code=(
                refusal.code.value if refusal is not None
                else blocks[0].code if blocks else None
            ),
            error_message=(
                refusal.message if refusal is not None
                else blocks[0].message if blocks else None
            ),
            decided_at=datetime.now(UTC) if status is OpeningStatus.REFUSED else None,
        )
        self._session.add(opening)
        await self._session.commit()
        logger.info(
            "Карточка открытия",
            extra={
                "user_id": self._user.id, "opening_id": opening.id, "status": status.value,
                "symbol": inputs.symbol, "side": inputs.side.value,
                "entry_type": inputs.entry_type.value, "code": opening.error_code,
            },
        )
        return opening

    # --- «Открыть» / «Отмена» --------------------------------------------------------

    async def load(self, opening_id: int) -> TradeOpening | None:
        opening: TradeOpening | None = await self._session.scalar(
            select(TradeOpening).where(
                TradeOpening.id == opening_id, TradeOpening.user_id == self._user.id
            )
        )
        return opening

    async def decline(self, opening_id: int) -> str:
        opening = await self.load(opening_id)
        if opening is None or opening.status is not OpeningStatus.CARD:
            return STATUS_TEXT.get(opening.status, "") if opening else "Карточка не найдена."
        await transition(
            self._session, opening, (OpeningStatus.CARD,), OpeningStatus.DECLINED,
            decided_at=datetime.now(UTC),
        )
        return "Отменено — на биржу ничего не отправлено."

    async def confirm(
        self,
        opening_id: int,
        *,
        accept_warnings: bool,
        message_id: int | None = None,
        runner_factory: Any = None,
        on_locked: OnLocked | None = None,
    ) -> ConfirmOutcome:
        """«Открыть» из чата или Mini App. Повторное нажатие, второй интерфейс
        и параллельный вызов отсекаются условным переходом CARD → CONFIRMED и
        локом позиции: вход отправляется один раз."""
        opening = await self.load(opening_id)
        if opening is None:
            return ConfirmOutcome("Карточка не найдена — открой заново.")
        if opening.status is not OpeningStatus.CARD:
            # Повторное нажатие: итог покажет (или уже показал) первое — карточку
            # не трогаем, ответ — подсказкой.
            return ConfirmOutcome(
                STATUS_TEXT.get(opening.status, "Карточка уже обработана."), opening.status,
                opening.trade_id, final=False,
            )
        if (
            message_id is not None and opening.card_message_id is not None
            and message_id != opening.card_message_id
        ):
            return ConfirmOutcome("Это не последняя карточка — открой заново.", final=False)
        now = datetime.now(UTC)
        if now - opening.created_at > timedelta(
            seconds=self._settings.exec_open_card_ttl_seconds
        ):
            await transition(
                self._session, opening, (OpeningStatus.CARD,), OpeningStatus.EXPIRED_CARD,
                decided_at=now, error_code=Code.CARD_EXPIRED.value,
            )
            return ConfirmOutcome(
                "⌛ Карточка устарела — цены могли уйти. Пересчитай.", OpeningStatus.EXPIRED_CARD
            )
        warnings = [v for v in opening.violations or [] if v.get("level") == Level.WARN.value]
        if warnings and not accept_warnings:
            return ConfirmOutcome(
                "Есть предупреждения — нажми «Открыть всё равно».", final=False
            )
        if self._redis is None:
            raise RuntimeError("OpeningService.confirm без Redis — лок позиции обязателен")
        key = position_lock_key(self._user.id, opening.symbol, opening.side.value)
        try:
            async with RedisLock(self._redis, key, self._settings.confirm_lock_ttl_seconds):
                if on_locked is not None:
                    await on_locked()
                return await self._confirm_locked(opening, bool(warnings), runner_factory)
        except LockBusyError:
            return ConfirmOutcome(BUSY_TEXT, final=False, busy=True)

    async def cancel_limit(
        self, opening_id: int, *, on_locked: OnLocked | None = None
    ) -> ConfirmOutcome:
        """«Отменить лимит» из чата или Mini App — та же ветка, что истечение."""
        opening = await self.load(opening_id)
        if opening is None or opening.status is not OpeningStatus.WORKING:
            status = opening.status if opening else None
            return ConfirmOutcome(
                STATUS_TEXT.get(status, "Лимит не найден.") if status else "Лимит не найден.",
                status, opening.trade_id if opening else None, final=False,
            )
        if self._redis is None:
            raise RuntimeError("OpeningService.cancel_limit без Redis — лок позиции обязателен")
        key = position_lock_key(self._user.id, opening.symbol, opening.side.value)
        try:
            async with RedisLock(self._redis, key, self._settings.confirm_lock_ttl_seconds):
                if on_locked is not None:
                    await on_locked()
                await self._session.refresh(opening)
                if opening.status is not OpeningStatus.WORKING:
                    return ConfirmOutcome(
                        STATUS_TEXT.get(opening.status, ""), opening.status, final=False
                    )
                client = await self._factory.for_user(
                    self._session, self._user.id, mode=opening.account_mode
                )
                try:
                    info = await MarketDataService(client, self._cache).get_symbol_info(
                        opening.symbol
                    )
                    runner = Runner(
                        self._session, self._settings, client, opening,
                        price_precision=info.price_precision if info else 8,
                        quantity_precision=info.quantity_precision if info else 8,
                    )
                    outcome = await limits.cancel(runner, OpeningStatus.CANCELLED)
                finally:
                    await client.close()
        except LockBusyError:
            return ConfirmOutcome(BUSY_TEXT, final=False, busy=True)
        return ConfirmOutcome(outcome.text, outcome.status, outcome.trade_id)

    async def close_alarm(
        self, opening_id: int, *, runner_factory: Any = None, on_locked: OnLocked | None = None
    ) -> ConfirmOutcome:
        """«🔴 Закрыть маркетом» → «Да, закрыть» под ALARM (A.1, решение владельца
        09.10): аварийное закрытие ядром открытия под локом позиции, итог —
        EMERGENCY_CLOSED со сделкой. Статус перепроверяется под локом: стоп мог
        встать циклом или быстрым повтором — тогда закрывать не нужно."""
        opening = await self.load(opening_id)
        if opening is None or opening.status is not OpeningStatus.ALARM:
            return _alarm_gone(opening)
        if self._redis is None:
            raise RuntimeError("OpeningService.close_alarm без Redis — лок позиции обязателен")
        key = position_lock_key(self._user.id, opening.symbol, opening.side.value)
        # Фоновые повторы защиты держат лок по 3–5 с (🔴 10.10): кнопка ждёт,
        # а новые повторы и цикл, видя ключ, уступают ей (деплой 3, фикс 5).
        wait = self._settings.exec_open_close_lock_wait_seconds
        wanted = close_wanted_key(opening.id)
        await self._redis.set(wanted, "1", ex=math.ceil(wait) + 4)
        try:
            async with RedisLock(
                self._redis, key, self._settings.confirm_lock_ttl_seconds, wait_seconds=wait
            ):
                await self._redis.delete(wanted)
                if on_locked is not None:
                    await on_locked()
                await self._session.refresh(opening)
                if opening.status is not OpeningStatus.ALARM:
                    return _alarm_gone(opening)
                client = await self._factory.for_user(
                    self._session, self._user.id, mode=opening.account_mode
                )
                try:
                    info = await MarketDataService(client, self._cache).get_symbol_info(
                        opening.symbol
                    )
                    runner = (runner_factory or Runner)(
                        self._session, self._settings, client, opening,
                        price_precision=info.price_precision if info else 8,
                        quantity_precision=info.quantity_precision if info else 8,
                    )
                    position = await runner.position()
                    if position is None:
                        return ConfirmOutcome(
                            "Позиции на бирже уже нет — закрывать нечего. Итог запишет "
                            "цикл открытий или сверка.", OpeningStatus.ALARM, final=False,
                        )
                    logger.warning(
                        "Закрываю позицию тревоги по кнопке владельца", extra=runner._log()
                    )
                    result = await runner._emergency(
                        position, EXIT_REASON_MANUAL, manual=True
                    )
                    outcome = await after_protect(runner, result)
                finally:
                    await client.close()
        except LockBusyError:
            logger.warning(
                "«Да, закрыть»: лок позиции не освободился", extra={
                    "user_id": self._user.id, "opening_id": opening.id, "wait_s": wait,
                },
            )
            return ConfirmOutcome(BUSY_TEXT, final=False, busy=True)
        finally:
            await self._redis.delete(wanted)
        if outcome.status is OpeningStatus.ALARM:
            return ConfirmOutcome(
                "⛔ Закрыть маркетом не удалось. " + outcome.text, OpeningStatus.ALARM
            )
        return ConfirmOutcome(outcome.text, outcome.status, outcome.trade_id)

    async def _refuse(
        self, opening: TradeOpening, code: str, message: str
    ) -> ConfirmOutcome:
        await transition(
            self._session, opening, (OpeningStatus.CONFIRMED,), OpeningStatus.REFUSED,
            error_code=code, error_message=message,
        )
        logger.info(
            "Открытие отклонено при «Открыть»",
            extra={"user_id": self._user.id, "opening_id": opening.id, "code": code},
        )
        return ConfirmOutcome(render_refusal(message), OpeningStatus.REFUSED)

    async def _confirm_locked(
        self, opening: TradeOpening, warnings_accepted: bool, runner_factory: Any
    ) -> ConfirmOutcome:
        if not await transition(
            self._session, opening, (OpeningStatus.CARD,), OpeningStatus.CONFIRMED,
            decided_at=datetime.now(UTC), warnings_accepted=warnings_accepted,
        ):
            return ConfirmOutcome("Уже открываю…", final=False)
        refusal = self._gate()
        if refusal is not None:
            return await self._refuse(opening, refusal.code.value, refusal.message)
        client, refusal = await self.client()
        if client is None:
            assert refusal is not None
            return await self._refuse(opening, refusal.code.value, refusal.message)
        try:
            return await self._execute(opening, client, runner_factory)
        finally:
            await client.close()

    async def _execute(
        self, opening: TradeOpening, client: ExchangeClient, runner_factory: Any
    ) -> ConfirmOutcome:
        inputs = inputs_of(opening)
        market, refusal = await self.market_snapshot(client, inputs)
        if market is None:
            assert refusal is not None
            return await self._refuse(opening, refusal.code.value, refusal.message)
        calc, issues = await self.evaluate(inputs, market)
        blocks = [i for i in issues if i.level is Level.BLOCK]
        if blocks:
            return await self._refuse(opening, blocks[0].code, blocks[0].message)
        card_warns = {v.get("code") for v in opening.violations or []}
        new_warns = [i for i in issues if i.level is Level.WARN and i.code not in card_warns]
        if new_warns:
            return await self._refuse(
                opening, Code.CARD_STALE.value,
                f"Появилось новое предупреждение: {new_warns[0].message} Пересчитай карточку.",
            )
        drift = self._drift(opening, calc)
        if drift is not None:
            return await self._refuse(opening, Code.CARD_STALE.value, drift)

        if self._settings.exec_open_dry_run:
            return await self._dry_run(opening, calc, market.symbol_info)

        refusal_text = await self._margin_and_leverage(client, opening, market)
        if refusal_text is not None:
            code, message = refusal_text
            return await self._refuse(opening, code, message)

        info = market.symbol_info
        runner = (runner_factory or Runner)(
            self._session, self._settings, client, opening,
            price_precision=info.price_precision, quantity_precision=info.quantity_precision,
        )
        entry = await runner.place_entry(calc.quantity)
        if entry.status is OpeningStatus.REJECTED:
            return ConfirmOutcome(
                f"⛔ Биржа отклонила вход: {entry.message}\nПозиция не открыта.",
                OpeningStatus.REJECTED,
            )
        if entry.status is OpeningStatus.WORKING:
            expires = (
                datetime.now(UTC) + timedelta(minutes=opening.expiry_minutes)
                if opening.expiry_minutes else None
            )
            await transition(
                self._session, opening, (OpeningStatus.SUBMITTING,), OpeningStatus.WORKING,
                expires_at=expires,
            )
            return ConfirmOutcome(working_text(runner), OpeningStatus.WORKING)
        if entry.status is OpeningStatus.UNKNOWN and opening.status is not OpeningStatus.UNKNOWN:
            # Вход не отправлен (уже отправлялся / открытие взяли) — ничего не делаем.
            return ConfirmOutcome(entry.message or "Уже открываю…", opening.status, final=False)
        outcome: FlowOutcome = await advance(runner)
        return ConfirmOutcome(outcome.text, outcome.status, outcome.trade_id)

    def _drift(self, opening: TradeOpening, calc: OpeningCalc) -> str | None:
        """Цена ушла с карточки: объём или риск $ изменились больше порога."""
        limit = self._settings.exec_open_card_drift_percent
        for name, before, after in (
            ("объём", opening.quantity, calc.quantity),
            ("риск", opening.risk_usd, calc.risk_usd),
        ):
            if before is None or before <= 0:
                continue
            change = abs(after - before) / before * Decimal(100)
            if change > limit:
                return (
                    f"Цена ушла с карточки: {name} изменился на "
                    f"{change.quantize(Decimal('0.1'))}% (порог {limit}%). Пересчитай карточку."
                )
        return None

    async def _dry_run(
        self, opening: TradeOpening, calc: OpeningCalc, info: SymbolInfo
    ) -> ConfirmOutcome:
        """EXEC_OPEN_DRY_RUN: строки того, что ушло бы, — на биржу ничего."""
        side = opening.side
        is_limit = opening.entry_type is EntryType.LIMIT
        rows = [
            (OrderRole.ENTRY, OrderType.LIMIT if is_limit else OrderType.MARKET,
             OPENING_SIDE[side], "e", opening.limit_price if is_limit else None, None),
            (OrderRole.STOP_LOSS, OrderType.STOP_MARKET, CLOSING_SIDE[side], "s", None,
             opening.stop_loss),
        ]
        if opening.take_profit is not None:
            rows.append((OrderRole.TAKE_PROFIT, OrderType.TAKE_PROFIT_MARKET, CLOSING_SIDE[side],
                         "t", None, opening.take_profit))
        for role, order_type, order_side, letter, price, trigger in rows:
            self._session.add(ExecutionOrder(
                user_id=self._user.id, trade_opening_id=opening.id,
                client_order_id=opening_client_order_id(opening.id, self._user.id, letter),
                symbol=opening.symbol, side=order_side, position_side=side,
                order_type=order_type, role=role, quantity=calc.quantity, price=price,
                trigger_price=trigger, card_price=opening.card_price, leverage=opening.leverage,
                status=OrderStatus.DRY_RUN, stage="confirm",
            ))
        await self._session.flush()
        await transition(
            self._session, opening, (OpeningStatus.CONFIRMED,), OpeningStatus.DRY_RUN,
            quantity=calc.quantity,
        )
        pp = info.price_precision
        kind = (
            f"лимит @ {fmt_price(opening.limit_price, pp)}" if is_limit else "маркет"
        )
        take = (
            f" и тейком {fmt_price(opening.take_profit, pp)}"
            if opening.take_profit is not None else ""
        )
        return ConfirmOutcome(
            f"🧪 Сухой прогон: {kind} {opening.symbol} {side.value} "
            f"{fmt_qty(calc.quantity, info.quantity_precision)} со стопом "
            f"{fmt_price(opening.stop_loss, pp)}{take} ушёл бы на биржу. Ничего не отправлено.",
            OpeningStatus.DRY_RUN,
        )

    async def _margin_and_leverage(
        self, client: ExchangeClient, opening: TradeOpening, market: MarketSnapshot
    ) -> tuple[str, str] | None:
        """Режим маржи и плечо — на бирже до входа, с read-back. Расхождение —
        отказ, вход не отправляется (разведка Р1–Р3а)."""
        symbol, side = opening.symbol, opening.side
        desired = market.desired_margin_type
        try:
            if market.margin_type is not desired:
                await client.set_margin_type(symbol, desired)
                actual = await client.get_margin_type(symbol, max_retries=1)
                if actual is not desired:
                    return (
                        "MARGIN_MODE_FAILED",
                        f"Режим маржи {symbol} не сменился: на бирже {actual.value}. "
                        "Вход не отправлен.",
                    )
            info = await client.get_leverage(symbol, max_retries=1)
            current = info.long_leverage if side.direction > 0 else info.short_leverage
            if current != opening.leverage:
                await client.set_leverage(symbol, opening.leverage, position_side=side.value)
                info = await client.get_leverage(symbol, max_retries=1)
                current = info.long_leverage if side.direction > 0 else info.short_leverage
                if current != opening.leverage:
                    return (
                        Code.LEVERAGE_FAILED.value,
                        f"Плечо не выставилось: {current}x вместо {opening.leverage}x. "
                        "Вход не отправлен.",
                    )
        except ExchangeError as exc:
            return (
                Code.LEVERAGE_FAILED.value,
                f"Режим маржи или плечо не выставлены ({exc}). Вход не отправлен.",
            )
        return None

    async def supersede_cards(
        self, keep_opening_id: int | None, keep_message_id: int | None = None
    ) -> list[tuple[int, int]]:
        """Новая карточка показана — прежние карточки пользователя (CARD и
        отказные с кнопками «Изменить/Отмена», за 2 часа) больше не действуют:
        CARD → EXPIRED_CARD (SUPERSEDED). Возвращает (chat_id, message_id), с
        которых чат снимает кнопки; сообщение новой карточки не трогается."""
        since = datetime.now(UTC) - timedelta(hours=2)
        rows = list(await self._session.scalars(
            select(TradeOpening).where(
                TradeOpening.user_id == self._user.id,
                TradeOpening.status.in_((OpeningStatus.CARD, OpeningStatus.REFUSED)),
                TradeOpening.card_message_id.is_not(None),
                TradeOpening.created_at >= since,
            )
        ))
        stale: list[tuple[int, int]] = []
        for row in rows:
            if row.id == keep_opening_id:
                continue
            if row.status is OpeningStatus.CARD:
                await transition(
                    self._session, row, (OpeningStatus.CARD,), OpeningStatus.EXPIRED_CARD,
                    error_code="SUPERSEDED",
                    error_message="Показана новая карточка — эта больше не действует.",
                )
            if (
                row.chat_id is not None and row.card_message_id is not None
                and row.card_message_id != keep_message_id
            ):
                stale.append((row.chat_id, row.card_message_id))
        return stale

    async def attach_message(self, opening: TradeOpening, chat_id: int, message_id: int) -> None:
        """Карточка отправлена отдельным сообщением — «Открыть» сверяет его id."""
        opening.chat_id = chat_id
        opening.card_message_id = message_id
        await self._session.commit()


def _with_issues(calc: OpeningCalc, issues: tuple[Issue, ...]) -> OpeningCalc:
    """Тот же расчёт с нарушениями плана — для текста карточки."""
    return replace(calc, issues=issues)


__all__ = [
    "LIMIT_EXPIRY_CHOICES",
    "CardOutcome",
    "OpeningService",
    "desired_margin_type",
]
