"""Оркестратор исполнения (этап 15.4, раздел 1 ТЗ).

evaluate() собирает контекст (цена, баланс, инструмент, открытые позиции,
дневной PnL — всё, что guards.py и sizing.py сами не умеют достать),
вызывает run_guards(), затем calculate_size(), и возвращает либо готовый
OrderRequest (обёрнутый в ExecutionQuote вместе с тем, что нужно карточке
для отображения), либо ExecutionRefusal. Отправки на биржу здесь нет и в
этом шаге не будет — это appears только в 15.5.

Дешёвые guard-и (EXECUTION_DISABLED, NO_TRADING_KEY) проверяются до
похода в БД и на биржу: если результат уже предрешён, незачем тратить
запрос к бирже ради баланса, который всё равно не понадобится.

Раздел 12а ТЗ: каждый отказ (любого гварда, в т.ч. двух дешёвых выше)
здесь же пишет строку-наблюдение в execution_orders (status REFUSED) —
это единственное место, которое знает код гварда и то немногое, что уже
успело стать известным к моменту отказа (цена/дрейф, если тикер уже
запрошен). Успешный путь (ExecutionQuote) ничего не пишет: итог карточки
(подтверждена/отклонена/просрочена) решается позже и не в этом методе —
см. app/bot/handlers/execution.py.

Шаг 15.5.2а: вход строится из снимка уведомления (SignalNotification) —
направление, вход, стоп, тейк — а не из строки слота, которую сканер
перезаписывает. Слот нужен только чтобы проверить, что снимок ещё
актуален (гварды 3а-4а, до первого запроса к бирже).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models.execution_order import ExecutionOrder
from app.database.models.signal import SignalRecord
from app.database.models.signal_notification import SignalNotification
from app.database.models.trading_plan import TradingPlan
from app.database.models.user import User
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.database.repositories.signal_notification import SignalNotificationRepository
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import ExchangeClient, ExchangeError, SymbolInfo, TpSlSpec
from app.execution.guards import (
    GuardInputs,
    check_execution_enabled,
    check_live_orders_allowed,
    check_mode_allowed,
    check_permissions_trustworthy,
    check_position_mode_known,
    check_trading_key,
    run_guards,
    run_signal_identity_guards,
)
from app.execution.leverage import leverage_needs_update
from app.execution.models import ExecutionRefusal, ExecutionRefusalCode, OrderRequest
from app.execution.sizing import calculate_size
from app.market.data import MarketDataService
from app.trading.calculations import PERCENT_PRECISION, calculate_risk_reward
from app.trading.enums import (
    ExchangeKeyMode,
    ObservationStage,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalDirection,
    SignalRecordStatus,
    TradeSide,
)
from app.trading.risk import day_bounds, tz_offset_for

ZERO = Decimal(0)
logger = get_logger(__name__)


class _EntryZone(Protocol):
    """entry-зона есть и у снимка уведомления, и у строки слота — путь
    исполнения передаёт снимок (шаг 15.5.2а)."""

    @property
    def entry_low(self) -> Decimal | None: ...

    @property
    def entry_high(self) -> Decimal | None: ...


def signal_reference_price(signal: _EntryZone) -> Decimal | None:
    """Опорная цена сигнала для дрейфа — середина entry-зоны (раздел 5 ТЗ:
    карточка показывает "сигнал был на ...")."""
    if signal.entry_low is not None and signal.entry_high is not None:
        return (signal.entry_low + signal.entry_high) / 2
    return signal.entry_low or signal.entry_high


def price_drift_percent(current_price: Decimal, signal: _EntryZone) -> Decimal | None:
    """Дрейф текущей цены от опорной цены сигнала — та же величина, что
    показана на карточке (раздел 5) и копится в сводке (раздел 12а)."""
    reference = signal_reference_price(signal)
    if reference is None or reference <= ZERO:
        return None
    return (abs(current_price - reference) / reference * Decimal(100)).quantize(
        PERCENT_PRECISION
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionQuote:
    """Успешный результат evaluate() — то, что нужно карточке подтверждения
    (раздел 5 ТЗ) сверх самого OrderRequest: срез "было до входа"."""

    order: OrderRequest
    open_positions_count: int
    current_total_risk_percent: Decimal
    symbol_info: SymbolInfo
    # Раздел 16 ТЗ, шаг 15.5.1: снят один раз при построении карточки
    # (см. app/bot/handlers/execution.py:_build_quote), на «Да» не
    # перезапрашивается — несётся отсюда дальше, тем же принципом, что
    # planned_price в _ConfirmationState.
    dual_side_position: bool

    @property
    def total_risk_after_percent(self) -> Decimal:
        return self.current_total_risk_percent + self.order.risk_percent


class ExecutionService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        client: ExchangeClient,
        market: MarketDataService,
    ) -> None:
        self._settings = settings
        self._client = client
        self._market = market
        self._session = session
        self._trades = TradeRepository(session)
        self._orders = ExecutionOrderRepository(session)

    async def _refuse(
        self,
        refusal: ExecutionRefusal,
        *,
        user_id: int,
        signal_id: int,
        notification_id: int,
        symbol: str,
        side: OrderSide,
        position_side: TradeSide,
        price: Decimal | None,
        drift: Decimal | None,
        stage: ObservationStage,
    ) -> ExecutionRefusal:
        """Раздел 12а ТЗ: одна строка-наблюдение на каждый отказ гварда,
        независимо от того, успел ли код добраться до цены/дрейфа. stage —
        на каком вызове evaluate() отказ случился (карточка ещё строится или
        уже показана и пришло «Да»)."""
        row = build_observation_order(
            user_id=user_id,
            signal_id=signal_id,
            notification_id=notification_id,
            symbol=symbol,
            side=side,
            position_side=position_side,
            status=OrderStatus.REFUSED,
            price=price,
            price_drift_percent=drift,
            error_code=refusal.code.value,
            error_message=refusal.message,
            stage=stage,
        )
        self._orders.add(row)
        await self._orders.flush()
        return refusal

    async def adjust_leverage(
        self, *, order: OrderRequest, position_side: str
    ) -> ExecutionRefusal | None:
        """Раздел 16 ТЗ, шаг 15.5.2: плечо перед реальной отправкой — читает
        текущее (get_leverage) и, только при расхождении по нужной стороне
        (leverage_needs_update — раздел 16, шаг 15.5.1), меняет его
        (set_leverage). И чтение, и запись — в одном try/except: без
        надёжного текущего плеча дальше идти нельзя по той же причине, что
        и при сбое самой смены — план 15.5.2 называл явно только
        set_leverage, но то же рассуждение относится и к чтению, которое
        ему предшествует.

        Вызывается только на confirm-пути (после «Да», карточка уже
        показана) — stage всегда CONFIRM. Сырой текст ошибки биржи — только
        в лог, не в ExecutionRefusal.message: то идёт в чат пользователю
        (render_refusal), внешним текстам там не место."""
        try:
            current = await self._client.get_leverage(order.symbol, max_retries=1)
            if leverage_needs_update(current, order.leverage, order.position_side):
                await self._client.set_leverage(
                    order.symbol, order.leverage, position_side=position_side
                )
        except ExchangeError as exc:
            logger.warning(
                "Не удалось выставить плечо перед отправкой ордера",
                extra={
                    "user_id": order.user_id,
                    "signal_id": order.signal_id,
                    "symbol": order.symbol,
                    "error": str(exc),
                },
            )
            refusal = ExecutionRefusal(
                ExecutionRefusalCode.LEVERAGE_FAILED,
                "Не удалось выставить плечо перед входом. Ордер не отправлен.",
            )
            return await self._refuse(
                refusal,
                user_id=order.user_id,
                signal_id=order.signal_id,
                notification_id=order.notification_id,
                symbol=order.symbol,
                side=order.side,
                position_side=order.position_side,
                price=order.entry_price,
                drift=None,
                stage=ObservationStage.CONFIRM,
            )
        return None

    async def submit_entry_order(
        self, *, order: OrderRequest, position_side: str, entry_row: ExecutionOrder
    ) -> ExecutionOrder:
        """Раздел 16 ТЗ, шаг 15.5.2. entry_row уже вставлена и закоммичена
        ДО вызова этого метода (раздел 8 ТЗ: PENDING до HTTP) —
        build_entry_order_pending() строит её, вызывающий код
        (app/bot/handlers/execution.py) вставляет и коммитит явно, коммит
        после HTTP — тоже явно в вызывающем коде: раздел 8 ТЗ требует
        коммит и до, и после отправки, порядок нагляднее держать в
        хендлере, а не прятать внутри одного метода.

        REJECTED только когда exc.code задан и не 0 — биржа ответила
        явным отказом, ответ определённо получен. Иначе (ExchangeUnavailableError
        — ответа не было вовсе; или code отсутствует, например сбой разбора
        уже после code=0 в _parse_order/_to_decimal) — UNKNOWN: ордер мог
        пройти, мы не знаем и не притворяемся, что знаем."""
        try:
            result = await self._client.place_market_order(
                symbol=order.symbol,
                side=order.side,
                position_side=position_side,
                quantity=order.quantity,
                client_order_id=order.entry_client_order_id,
                take_profit=TpSlSpec(trigger_price=order.take_profit),
                stop_loss=TpSlSpec(trigger_price=order.stop_loss),
            )
        except ExchangeError as exc:
            logger.warning(
                "Ответ BingX на отправку ордера — отказ или сбой",
                extra={
                    "user_id": order.user_id,
                    "signal_id": order.signal_id,
                    "symbol": order.symbol,
                    "error": str(exc),
                    "code": exc.code,
                },
            )
            if exc.code is not None and exc.code != 0:
                entry_row.status = OrderStatus.REJECTED
                entry_row.error_code = str(exc.code)
            else:
                entry_row.status = OrderStatus.UNKNOWN
                entry_row.error_code = type(exc).__name__
            entry_row.raw_response = exc.payload
            return entry_row
        except Exception as exc:
            # Осознанное исключение из правила «не ловить Exception»: запрос
            # уже ушёл, и любой непредвиденный сбой после него (баг разбора
            # ответа, неожиданная форма data) означает ровно «исход
            # неизвестен» — UNKNOWN говорит правду, а пролетевшее исключение
            # оставило бы строку PENDING и показало «внутреннюю ошибку»
            # вместо «ордер мог пройти». Баг не прячется: полный трейс в лог.
            logger.exception(
                "Непредвиденный сбой после отправки ордера — исход неизвестен",
                extra={
                    "user_id": order.user_id,
                    "signal_id": order.signal_id,
                    "symbol": order.symbol,
                },
            )
            entry_row.status = OrderStatus.UNKNOWN
            entry_row.error_code = type(exc).__name__
            entry_row.raw_response = None
            return entry_row

        entry_row.status = OrderStatus.SUBMITTED
        if not result.order_id:
            # Биржа приняла (code 0), но orderId не прислала. Статус верный,
            # пустую строку вместо id не пишем — сверка пойдёт по
            # clientOrderID.
            logger.warning(
                "BingX не вернул orderId при code 0",
                extra={
                    "user_id": order.user_id,
                    "signal_id": order.signal_id,
                    "symbol": order.symbol,
                    "client_order_id": order.entry_client_order_id,
                },
            )
        entry_row.exchange_order_id = result.order_id or None
        entry_row.raw_response = result.raw
        return entry_row

    async def evaluate(
        self,
        *,
        user: User,
        notification: SignalNotification,
        slot: SignalRecord,
        plan: TradingPlan,
        has_trading_key: bool,
        key_can_trade_futures: bool,
        permissions_trustworthy: bool = True,
        dual_side_position: bool | None = None,
        selected_exchange_mode: ExchangeKeyMode,
        planned_price: Decimal | None = None,
        now: datetime | None = None,
    ) -> ExecutionQuote | ExecutionRefusal:
        """planned_price=None — первый показ карточки: PRICE_DRIFT сравнивается
        сам с собой (0) и не может отказать. При повторном вызове с "Да"
        planned_price — цена, зафиксированная на карточке (раздел 5).

        Пакет B: гвард SIGNAL_STALE не зависит от planned_price (сравнивает
        current_price с ценой сигнала, не с ценой карточки), поэтому
        работает одинаково на обоих вызовах — в т.ч. на первом, где
        PRICE_DRIFT структурно бессилен.

        Раздел 12а: planned_price не None — это вызов на «Да» (карточка уже
        показана), строки отказов получают stage=CONFIRM, иначе CARD.
        Пересчёт после PRICE_DRIFT идёт с planned_price=None — это
        построение новой карточки, то есть CARD.

        Шаг 15.5.2а: notification — снимок, из которого строится вход; slot —
        текущая строка слота (notification.signal_id), только для гвардов
        3а-4а. Они идут дёшево, сразу после MODE_NOT_ALLOWED и до первого
        запроса к бирже: сетап, обновившийся за время жизни карточки,
        отказывается на «Да» без единого HTTP-вызова."""
        moment = now or datetime.now(UTC)
        stage = ObservationStage.CONFIRM if planned_price is not None else ObservationStage.CARD
        # Раздел 8 ТЗ: путь подтверждения («Да») держит Redis-лок — там
        # транспортный повтор только удлиняет удержание лока и приближает
        # его к TTL без пользы (см. Settings.confirm_lock_ttl_seconds).
        # Сбой биржи там должен явиться быстро, как ExchangeError (пакет C
        # запишет ERROR и подберёт человеческий текст), а не после трёх
        # попыток. Путь построения карточки (CARD, включая пересчёт после
        # PRICE_DRIFT) лока не держит — там обычный повтор клиента уместен,
        # call_retries=None оставляет его как есть.
        call_retries = 1 if stage is ObservationStage.CONFIRM else None

        if (
            notification.direction is None
            or notification.stop_loss is None
            or notification.take_profit is None
        ):
            # Кнопка показывается только под READY (см. handlers/execution.py),
            # а у READY эти поля всегда заполнены сканером — попадание сюда
            # значит, что где-то выше пропустили проверку уровня уведомления.
            raise ValueError(
                f"Уведомление {notification.id} не READY: нет направления/уровней"
            )
        side = TradeSide(notification.direction.value)
        order_side = OrderSide.BUY if side is TradeSide.LONG else OrderSide.SELL
        stop_loss = notification.stop_loss
        take_profit = notification.take_profit
        symbol = slot.symbol

        async def refuse(
            refusal: ExecutionRefusal,
            *,
            price: Decimal | None = None,
            drift: Decimal | None = None,
        ) -> ExecutionRefusal:
            return await self._refuse(
                refusal,
                user_id=user.id,
                signal_id=slot.id,
                notification_id=notification.id,
                symbol=symbol,
                side=order_side,
                position_side=side,
                price=price,
                drift=drift,
                stage=stage,
            )

        if refusal := check_execution_enabled(
            execution_enabled=self._settings.trading_execution_enabled
        ):
            return await refuse(refusal)
        if refusal := check_live_orders_allowed(
            trading_mode=self._settings.bingx_trading_mode,
            allow_live_mode_orders=self._settings.exec_allow_live_mode_orders,
        ):
            # Сразу после EXECUTION_DISABLED, до похода за правами ключа
            # (раздел 16 ТЗ, шаг 15.5.1): LIVE без явного разрешения
            # конфигом отказан раньше, чем код успеет спросить биржу
            # хоть о чём-то.
            return await refuse(refusal)
        if refusal := check_permissions_trustworthy(trustworthy=permissions_trustworthy):
            # До check_trading_key: если права не удалось проверить,
            # key_can_trade_futures мог остаться устаревшим значением
            # (обновляется только при успешном refresh_permissions) — гонять
            # его через check_trading_key раньше, чем отказать этим кодом,
            # значит рисковать пропустить NO_TRADING_KEY вместо честного
            # "не знаем" или наоборот.
            return await refuse(refusal)
        if refusal := check_position_mode_known(known=dual_side_position is not None):
            return await refuse(refusal)
        # Гвард выше уже отказал бы на None — сюда доходим только со
        # значением. assert вместо cast: реальная проверка, а не просто
        # подсказка mypy, если инвариант вдруг нарушится выше.
        assert dual_side_position is not None
        if refusal := check_trading_key(
            has_key=has_trading_key, key_can_trade_futures=key_can_trade_futures
        ):
            return await refuse(refusal)
        allowed_exchange_mode = self._settings.bingx_allowed_exchange_mode
        if refusal := check_mode_allowed(
            selected_mode=selected_exchange_mode, allowed_mode=allowed_exchange_mode
        ):
            return await refuse(refusal)

        # Шаг 15.5.2а: 3а-4а — только своя БД, до первого запроса к бирже.
        slot_active = slot.status is SignalRecordStatus.ACTIVE
        setup_already_traded = await SignalNotificationRepository(
            self._session
        ).exists_traded(slot.id, notification.fingerprint)
        if refusal := run_signal_identity_guards(
            slot_active=slot_active,
            slot_fingerprint=slot.fingerprint,
            notification_fingerprint=notification.fingerprint,
            notification_expires_at=notification.expires_at,
            now=moment,
            notification_trade_opened_at=notification.trade_opened_at,
            setup_already_traded=setup_already_traded,
        ):
            return await refuse(refusal)

        ticker = await self._client.get_ticker(symbol, max_retries=call_retries)
        current_price = ticker.last_price
        if planned_price is None:
            planned_price = current_price
        drift = price_drift_percent(current_price, notification)

        symbol_info = await self._market.get_symbol_info(symbol, max_retries=call_retries)
        if symbol_info is None:
            return await refuse(
                ExecutionRefusal(
                    ExecutionRefusalCode.SYMBOL_DATA_UNAVAILABLE,
                    f"{symbol}: нет данных инструмента на бирже.",
                ),
                price=current_price,
                drift=drift,
            )

        balance = (await self._client.get_balance(max_retries=call_retries)).equity

        open_trades = await self._trades.list_open(user.id)
        open_positions_count = len(open_trades)
        has_open_position = any(t.symbol == symbol for t in open_trades)
        current_total_risk_percent = sum(
            (t.risk_percent for t in open_trades if t.risk_percent is not None), ZERO
        )

        day_start, day_end = day_bounds(moment, tz_offset_for(user.settings.timezone))
        day_pnl = await self._trades.sum_pnl_between(user.id, day_start, day_end)
        day_loss_percent = None
        if day_pnl is not None and balance > ZERO:
            day_loss_percent = max(ZERO, -day_pnl / balance * Decimal(100))

        leverage = plan.max_leverage

        guard_inputs = GuardInputs(
            execution_enabled=self._settings.trading_execution_enabled,
            bingx_trading_mode=self._settings.bingx_trading_mode,
            exec_allow_live_mode_orders=self._settings.exec_allow_live_mode_orders,
            has_trading_key=has_trading_key,
            key_can_trade_futures=key_can_trade_futures,
            selected_exchange_mode=selected_exchange_mode,
            allowed_exchange_mode=allowed_exchange_mode,
            slot_active=slot_active,
            slot_fingerprint=slot.fingerprint,
            notification_fingerprint=notification.fingerprint,
            notification_expires_at=notification.expires_at,
            now=moment,
            notification_trade_opened_at=notification.trade_opened_at,
            setup_already_traded=setup_already_traded,
            has_open_position=has_open_position,
            open_positions_count=open_positions_count,
            max_positions=self._settings.exec_max_open_positions,
            current_total_risk_percent=current_total_risk_percent,
            new_risk_percent=plan.risk_per_trade_percent,
            max_total_risk_percent=self._settings.exec_max_total_risk_percent,
            day_loss_percent=day_loss_percent,
            max_daily_loss_percent=plan.max_daily_loss_percent,
            planned_price=planned_price,
            current_price=current_price,
            max_price_drift_ratio=self._settings.exec_max_price_drift_ratio,
            signal_reference_price=signal_reference_price(notification),
            max_signal_staleness_ratio=self._settings.exec_max_signal_staleness_ratio,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            side=side,
            min_risk_reward=self._settings.exec_min_rr,
            account_balance=balance,
            leverage=leverage,
            symbol_info=symbol_info,
            symbol=symbol,
            symbol_whitelist=self._settings.exec_symbol_whitelist_symbols,
        )

        if refusal := run_guards(guard_inputs):
            return await refuse(refusal, price=current_price, drift=drift)

        sizing = calculate_size(
            account_balance=balance,
            risk_percent=plan.risk_per_trade_percent,
            entry_price=current_price,
            stop_loss=stop_loss,
            side=side,
            leverage=leverage,
            symbol_info=symbol_info,
        )
        if isinstance(sizing, ExecutionRefusal):
            # run_guards() выше уже прогнал calculate_size() с теми же
            # аргументами (раздел 7, п.11) — второй отказ здесь возможен
            # только при гонке (баланс/лот изменились между двумя
            # вызовами), не в штатном пути.
            return await refuse(sizing, price=current_price, drift=drift)

        risk_reward = calculate_risk_reward(
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            side=side,
        )
        risk_percent = (
            (sizing.risk_amount / balance * Decimal(100)).quantize(PERCENT_PRECISION)
            if balance > ZERO
            else ZERO
        )

        order = OrderRequest(
            user_id=user.id,
            signal_id=slot.id,
            notification_id=notification.id,
            symbol=symbol,
            side=OrderSide.BUY if side is TradeSide.LONG else OrderSide.SELL,
            position_side=side,
            quantity=sizing.quantity,
            entry_price=current_price,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            notional=sizing.notional,
            margin=sizing.margin,
            risk_amount=sizing.risk_amount,
            risk_percent=risk_percent,
            risk_reward=risk_reward,
        )

        return ExecutionQuote(
            order=order,
            open_positions_count=open_positions_count,
            current_total_risk_percent=current_total_risk_percent,
            symbol_info=symbol_info,
            dual_side_position=dual_side_position,
        )


def build_execution_orders(
    order: OrderRequest,
    status: OrderStatus,
    *,
    price_drift_percent: Decimal | None = None,
) -> list[ExecutionOrder]:
    """Раздел 8 ТЗ: один вход — три строки execution_orders (вход, стоп,
    тейк), каждая со своим client_order_id. На 15.4 status=DRY_RUN —
    строки фиксируют намерение, не отправку (появится в 15.5).

    Раздел 12а ТЗ: снимок расчёта (цена, дрейф, объём, маржа, плечо,
    риск, RR) пишется только на строку ENTRY — SL/TP-ноги того же входа
    его не повторяют, см. app/database/models/execution_order.py.
    """
    closing_side = OrderSide.SELL if order.side is OrderSide.BUY else OrderSide.BUY
    return [
        ExecutionOrder(
            user_id=order.user_id,
            signal_id=order.signal_id,
            notification_id=order.notification_id,
            client_order_id=order.entry_client_order_id,
            symbol=order.symbol,
            side=order.side,
            position_side=order.position_side,
            order_type=OrderType.MARKET,
            role=OrderRole.ENTRY,
            quantity=order.quantity,
            price=order.entry_price,
            status=status,
            price_drift_percent=price_drift_percent,
            notional=order.notional,
            margin=order.margin,
            leverage=order.leverage,
            risk_amount=order.risk_amount,
            risk_percent=order.risk_percent,
            risk_reward=order.risk_reward,
        ),
        ExecutionOrder(
            user_id=order.user_id,
            signal_id=order.signal_id,
            notification_id=order.notification_id,
            client_order_id=order.stop_loss_client_order_id,
            symbol=order.symbol,
            side=closing_side,
            position_side=order.position_side,
            order_type=OrderType.STOP_MARKET,
            role=OrderRole.STOP_LOSS,
            quantity=order.quantity,
            trigger_price=order.stop_loss,
            status=status,
        ),
        ExecutionOrder(
            user_id=order.user_id,
            signal_id=order.signal_id,
            notification_id=order.notification_id,
            client_order_id=order.take_profit_client_order_id,
            symbol=order.symbol,
            side=closing_side,
            position_side=order.position_side,
            order_type=OrderType.TAKE_PROFIT_MARKET,
            role=OrderRole.TAKE_PROFIT,
            quantity=order.quantity,
            trigger_price=order.take_profit,
            status=status,
        ),
    ]


def build_entry_order_pending(
    order: OrderRequest, *, price_drift_percent: Decimal | None = None
) -> ExecutionOrder:
    """Раздел 16 ТЗ, шаг 15.5.2 / раздел 8 ТЗ: ENTRY-строка со статусом
    PENDING для реальной отправки — вызывающий код обязан вставить и
    закоммитить её ДО HTTP-запроса на биржу (единственная гарантия
    идемпотентности — UNIQUE на client_order_id, раздел 8 ТЗ).

    В отличие от build_execution_orders() (DRY_RUN — сразу три строки)
    здесь только ENTRY: STOP_LOSS/TAKE_PROFIT execution_orders-строки в
    этот шаг не входят, появятся при read-back (15.5.3), когда станет
    известно, прикрепился ли стоп реально (stopPrice != 0) — раньше
    писать их нечем и не о чем."""
    return ExecutionOrder(
        user_id=order.user_id,
        signal_id=order.signal_id,
        notification_id=order.notification_id,
        client_order_id=order.entry_client_order_id,
        symbol=order.symbol,
        side=order.side,
        position_side=order.position_side,
        order_type=OrderType.MARKET,
        role=OrderRole.ENTRY,
        quantity=order.quantity,
        price=order.entry_price,
        status=OrderStatus.PENDING,
        price_drift_percent=price_drift_percent,
        notional=order.notional,
        margin=order.margin,
        leverage=order.leverage,
        risk_amount=order.risk_amount,
        risk_percent=order.risk_percent,
        risk_reward=order.risk_reward,
    )


def build_observation_order(
    *,
    user_id: int,
    signal_id: int,
    notification_id: int | None = None,
    symbol: str,
    side: OrderSide,
    position_side: TradeSide,
    status: OrderStatus,
    price: Decimal | None = None,
    price_drift_percent: Decimal | None = None,
    quantity: Decimal | None = None,
    notional: Decimal | None = None,
    margin: Decimal | None = None,
    leverage: int | None = None,
    risk_amount: Decimal | None = None,
    risk_percent: Decimal | None = None,
    risk_reward: Decimal | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    stage: ObservationStage | None = None,
) -> ExecutionOrder:
    """Раздел 12а ТЗ: одна строка на исход попытки входа, которая не
    дошла до полноценной тройки build_execution_orders() — отказ гварда
    (REFUSED), отказ пользователя (DECLINED) или просрочка карточки
    (EXPIRED). Без client_order_id: это не намерение отправить что-то на
    биржу, а только наблюдение за тем, что произошло."""
    return ExecutionOrder(
        user_id=user_id,
        signal_id=signal_id,
        notification_id=notification_id,
        symbol=symbol,
        side=side,
        position_side=position_side,
        order_type=OrderType.MARKET,
        role=OrderRole.ENTRY,
        status=status,
        price=price,
        price_drift_percent=price_drift_percent,
        quantity=quantity,
        notional=notional,
        margin=margin,
        leverage=leverage,
        risk_amount=risk_amount,
        risk_percent=risk_percent,
        risk_reward=risk_reward,
        error_code=error_code,
        error_message=error_message,
        stage=stage.value if stage is not None else None,
    )


def build_exchange_error_order(
    *,
    user_id: int,
    signal_id: int,
    notification_id: int | None = None,
    symbol: str,
    direction: SignalDirection,
    error: Exception,
    stage: ObservationStage,
) -> ExecutionOrder:
    """Раздел 12а: сбой биржи на пути входа (статус ERROR). error_code —
    имя класса исключения; error_message намеренно пуст: в str(exc) может
    быть тело ответа биржи (одна из ручек BingX отдаёт apiKey эхом), полный
    текст остаётся в логе."""
    position_side = TradeSide(direction.value)
    return build_observation_order(
        user_id=user_id,
        signal_id=signal_id,
        notification_id=notification_id,
        symbol=symbol,
        side=OrderSide.BUY if position_side is TradeSide.LONG else OrderSide.SELL,
        position_side=position_side,
        status=OrderStatus.ERROR,
        error_code=type(error).__name__,
        stage=stage,
    )


def build_observation_order_from_quote(
    order: OrderRequest, *, status: OrderStatus, price_drift_percent: Decimal | None
) -> ExecutionOrder:
    """DECLINED/EXPIRED: карточка была показана и до неё дошёл полноценный
    ExecutionQuote — переиспользуем его числа вместо пересчёта."""
    return build_observation_order(
        user_id=order.user_id,
        signal_id=order.signal_id,
        notification_id=order.notification_id,
        symbol=order.symbol,
        side=order.side,
        position_side=order.position_side,
        status=status,
        price=order.entry_price,
        price_drift_percent=price_drift_percent,
        quantity=order.quantity,
        notional=order.notional,
        margin=order.margin,
        leverage=order.leverage,
        risk_amount=order.risk_amount,
        risk_percent=order.risk_percent,
        risk_reward=order.risk_reward,
    )
