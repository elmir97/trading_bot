"""Открытие сделки из бота — сервис (docs/open-trade-plan.md, §2, §5).

Без aiogram: вызывают чат-мастер и Mini App. Здесь — данные с биржи, гварды,
расчёт карточки (calc + checks), снимок в trade_openings. Исполнение
(«Открыть») — confirm(), восстановление — recovery.

Гварды — те же, что у действий этапа 4: исполнение включено, режим счёта
настроек совпадает с разрешённым конфигом, ключ с правом торговли, хедж-режим.
Плюс свой: открытие на LIVE — только с EXEC_OPEN_ALLOW_LIVE.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.exchanges.base import ExchangeClient, ExchangeError, MarginType
from app.execution import guards
from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.opening import checks
from app.execution.opening.calc import (
    Issue,
    Level,
    Limits,
    MarketSnapshot,
    OpeningCalc,
    OpeningInputs,
    compute,
)
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
            ttl_seconds=self._settings.exec_confirm_ttl_seconds,
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
