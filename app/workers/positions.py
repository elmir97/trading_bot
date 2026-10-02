"""Монитор приближения позиций биржи к SL/TP (этап 5).

Источник — позиции биржи, а не сделки журнала: вход — средняя цена позиции,
уровни — стоп и тейк из openOrders (при ручной лестнице — ближайший к цене).
Сделки только из журнала уведомлений не получают (решение владельца 02.10).

Частота: позиции и ордера — раз в position_monitor_snapshot_seconds (60 с,
get_positions + get_open_orders без символа на пользователя), mark price —
каждый цикл, раз в position_monitor_price_seconds (15 с, один публичный
запрос на символ). Пока жив любой exec:lock:* — цикл пропускается, как у
reconciler: лимиты BingX — пути «Да».

Порог — свой у пользователя, отдельно для SL и TP (user_settings.sl/
tp_alert_percent, NULL — 80% пути от входа к уровню), каждый выключается
(notifications sl_approaching / tp_approaching).

Дедуп — по уровню: строка position_alerts (пользователь, символ, сторона,
SL/TP, цена уровня) ставится только после окончательного исхода доставки
(delivery.final). Перенос уровня — новый ключ; старые ключи позиции, уровня
которых больше нет, и ключи исчезнувших позиций удаляются при обновлении
снимка. Повтор по тому же уровню — только после ухода назад за порог минус
HYSTERESIS (80% → сброс ниже 60%) и нового подхода.

Чтобы понять, хватает ли 15 секунд, в лог пишется, сколько пути было
пройдено в момент уведомления (перескок за порог, п.п.).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.numfmt import fmt_money, fmt_price
from app.core.security import SecretCipher
from app.database.models.position_alert import PositionAlert
from app.database.models.user import DEFAULT_ALERT_PERCENT
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ExchangeAuthError, ExchangeError
from app.execution.position_actions import DISCLAIMER, breakeven_price
from app.execution.position_view import PositionView, build_views
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.trading.enums import TradeSide
from app.workers.notifier import approach_enabled, send_notification

logger = get_logger(__name__)

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
# Отметка снимается, когда пройдено меньше порога минус HYSTERESIS доли пути
# (80% → ниже 60%): гистерезис против дребезга у края полосы.
HYSTERESIS = Decimal("0.20")
LOCK_PATTERN = "exec:lock:*"
PULSE_EVERY = timedelta(hours=1)

# callback_data кнопок — те же, что у экрана «Позиции» и карточек этапа 4
# (app/bot/handlers/position_actions.ActionCB.OPEN, positions.PositionsCB.
# ACTIONS). Воркеры app.bot не импортируют; совпадение проверяет тест.
ACTION_OPEN = "pa:"
POSITION_ACTIONS = "pos:act:"
SIDE_CODE = {TradeSide.LONG: "L", TradeSide.SHORT: "S"}
KINDS = ("SL", "TP")


def progress_fraction(entry: Decimal, target: Decimal, price: Decimal) -> Decimal | None:
    """Доля пути от входа к цели, пройденная текущей ценой.

    0 — цена ещё у входа, 1 — цена дошла до цели. Работает и для TP, и для
    SL, и для LONG, и для SHORT одинаково: знак (target - entry) сам
    отражает направление, поэтому сторону сделки передавать не нужно.
    """
    total = target - entry
    if total == ZERO:
        return None
    return (price - entry) / total


@dataclass(frozen=True, slots=True)
class UserSnapshot:
    """Снимок позиций пользователя на момент обновления (раз в 60 с). ORM
    объектов не держит — между циклами сессии нет."""

    user_id: int
    telegram_id: int
    sl_on: bool
    tp_on: bool
    sl_percent: int
    tp_percent: int
    views: tuple[PositionView, ...]
    # (symbol, side) → |вход − стоп R| на единицу (Trade.risk_stop журнала);
    # нет сделки или стопа — None, R «н/д».
    r_unit: dict[tuple[str, TradeSide], Decimal | None]
    precision: dict[str, int]


def level_of(view: PositionView, kind: str) -> Decimal | None:
    """Стоп / тейк позиции; при лестнице — ближайший к цене."""
    orders = view.stops if kind == "SL" else view.takes
    return orders[0].trigger_price if orders else None


def render_alert(
    view: PositionView, kind: str, level: Decimal, mark: Decimal,
    r_unit: Decimal | None, precision: int | None,
) -> str:
    p = view.position
    sign = ONE if p.side is TradeSide.LONG else -ONE
    icon, name = ("🛑", "стопу") if kind == "SL" else ("🎯", "тейку")
    level_name = "стоп" if kind == "SL" else "тейк"
    left = abs(level - mark)
    left_pct = (left / mark * HUNDRED).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    passed = progress_fraction(p.entry_price, level, mark)
    beyond = passed is not None and passed > ONE
    pnl = (mark - p.entry_price) * p.quantity * sign

    def r(value: Decimal) -> str:
        if not r_unit:
            return "н/д"
        return f"{(value / r_unit).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}R"

    distance = (
        f"уровень по mark уже пройден на {left_pct:f}%" if beyond
        else f"осталось {left_pct:f}% ({r(left)})"
    )
    return "\n".join([
        f"{icon} <b>{p.symbol} {p.side.value}</b> приближается к {name}",
        f"Mark {fmt_price(mark, precision)} · {level_name} {fmt_price(level, precision)} — "
        f"{distance}",
        f"PnL {fmt_money(pnl)} USDT ({r(pnl / p.quantity) if p.quantity else 'н/д'})",
        "",
        DISCLAIMER,
    ])


def alert_keyboard(
    view: PositionView, mark: Decimal, *, fee_rate: Decimal, min_distance_percent: Decimal,
    precision: int | None,
) -> InlineKeyboardMarkup:
    """Кнопки — входы в карточки этапа 4 (строятся заново с биржи в момент
    нажатия). «Стоп в безубыток» — только если mark уже за безубытком с
    запасом минимальной дистанции стопа, иначе карточка всё равно откажет."""
    p = view.position
    tail = f"{p.symbol}:{SIDE_CODE[p.side]}"
    rows: list[list[InlineKeyboardButton]] = []
    if precision is not None:
        be = breakeven_price(p.side, p.entry_price, fee_rate, p.entry_price * fee_rate, precision)
        room = min_distance_percent / HUNDRED
        if p.side is TradeSide.LONG:
            beyond = mark >= be * (ONE + room)
        else:
            beyond = mark <= be * (ONE - room)
        if beyond:
            rows.append([InlineKeyboardButton(
                text="🛡 Стоп в безубыток", callback_data=f"{ACTION_OPEN}be:{tail}"
            )])
    rows.append([
        InlineKeyboardButton(text="❌ Закрыть всё", callback_data=f"{ACTION_OPEN}cf:{tail}"),
        InlineKeyboardButton(text="⚙️ Действия", callback_data=f"{POSITION_ACTIONS}{tail}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class PositionMonitor:
    def __init__(
        self, bot: Bot, db: Database, settings: Settings, cipher: SecretCipher | None,
        redis: Any = None, *, factory: Any = None, market: MarketDataService | None = None,
    ) -> None:
        self._bot = bot
        self._db = db
        self._settings = settings
        self._redis = redis
        self._factory = factory or ExchangeFactory(settings, cipher)  # type: ignore[arg-type]
        if market is None:
            client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
            market = MarketDataService(client, TTLCache())
        self._market = market
        self._snapshots: list[UserSnapshot] = []
        self._snapshot_at: datetime | None = None
        self._pulse_at = datetime.now(UTC)
        self._stats = {"cycles": 0, "skipped": 0, "alerts": 0, "errors": 0}

    async def run(self) -> None:
        now = datetime.now(UTC)
        self._pulse(now)
        if await self._lock_in_flight():
            self._stats["skipped"] += 1
            logger.info("Монитор позиций пропущен: идёт действие с позицией (exec:lock)")
            return
        self._stats["cycles"] += 1
        snapshot_every = timedelta(seconds=self._settings.position_monitor_snapshot_seconds)
        if self._snapshot_at is None or now - self._snapshot_at >= snapshot_every:
            await self._refresh(now)
        if not any(s.views for s in self._snapshots):
            return
        symbols = sorted({v.position.symbol for s in self._snapshots for v in s.views})
        prices = await self._market.get_mark_prices(symbols)
        async with self._db.session() as session:
            for snap in self._snapshots:
                for view in snap.views:
                    mark = prices.get(view.position.symbol)
                    if mark is None:
                        continue
                    for kind in KINDS:
                        try:
                            await self._check(session, snap, view, kind, mark)
                        except Exception:
                            self._stats["errors"] += 1
                            logger.exception(
                                "Проверка приближения упала",
                                extra={"symbol": view.position.symbol, "kind": kind},
                            )

    def _pulse(self, now: datetime) -> None:
        if now - self._pulse_at < PULSE_EVERY:
            return
        positions = sum(len(s.views) for s in self._snapshots)
        logger.info(
            f"Пульс монитора позиций: циклов {self._stats['cycles']}, пропущено по локу "
            f"{self._stats['skipped']}, позиций {positions}, уведомлений "
            f"{self._stats['alerts']}, ошибок {self._stats['errors']}"
        )
        self._pulse_at = now
        self._stats = dict.fromkeys(self._stats, 0)

    async def _lock_in_flight(self) -> bool:
        if self._redis is None:
            return False
        async for _key in self._redis.scan_iter(match=LOCK_PATTERN, count=100):
            return True
        return False

    # --- снимок позиций (раз в 60 с) ---------------------------------------

    async def _refresh(self, now: datetime) -> None:
        snapshots: list[UserSnapshot] = []
        async with self._db.session() as session:
            for user in await UserRepository(session).list_active_with_plan():
                st = user.settings
                if st is None:
                    continue
                sl_on = approach_enabled(st, "sl_approaching")
                tp_on = approach_enabled(st, "tp_approaching")
                if not (sl_on or tp_on):
                    continue
                try:
                    client = await self._factory.for_user(
                        session, user.id, mode=st.active_exchange_mode
                    )
                except ExchangeAuthError:
                    continue
                try:
                    positions = await client.get_positions()
                    orders = await client.get_open_orders() if positions else []
                except ExchangeError:
                    # Снимок пользователя не обновлён — уведомлений по нему в
                    # эту минуту нет, отметки не трогаем.
                    self._stats["errors"] += 1
                    logger.warning("Монитор позиций: биржа не ответила",
                                   extra={"user_id": user.id})
                    continue
                finally:
                    await client.close()
                trades = await TradeRepository(session).list_open(user.id)
                views = build_views(positions, orders, trades)
                r_unit: dict[tuple[str, TradeSide], Decimal | None] = {}
                for view in views:
                    stop = view.trade.risk_stop if view.trade is not None else None
                    r_unit[(view.position.symbol, view.position.side)] = (
                        abs(view.position.entry_price - stop) or None if stop is not None else None
                    )
                precision = await self._precision({v.position.symbol for v in views})
                await self._prune(session, user.id, views)
                snapshots.append(UserSnapshot(
                    user_id=user.id, telegram_id=user.telegram_id, sl_on=sl_on, tp_on=tp_on,
                    sl_percent=st.sl_alert_percent or DEFAULT_ALERT_PERCENT,
                    tp_percent=st.tp_alert_percent or DEFAULT_ALERT_PERCENT,
                    views=tuple(views), r_unit=r_unit, precision=precision,
                ))
        self._snapshots = snapshots
        self._snapshot_at = now

    async def _precision(self, symbols: set[str]) -> dict[str, int]:
        if not symbols:
            return {}
        try:
            infos = await self._market.get_symbols()
        except Exception:
            logger.warning("Монитор позиций: точность цен не получена")
            return {}
        return {i.symbol: i.price_precision for i in infos if i.symbol in symbols}

    async def _prune(self, session: AsyncSession, user_id: int, views: list[PositionView]) -> None:
        """Ключи, уровня которых больше нет (перенос, снятие, позиция
        исчезла), удаляются: новый подход к новому уровню — новое
        уведомление."""
        live = {
            (v.position.symbol, v.position.side, kind, level)
            for v in views for kind in KINDS
            if (level := level_of(v, kind)) is not None
        }
        rows = await session.scalars(select(PositionAlert).where(PositionAlert.user_id == user_id))
        stale = [
            r.id for r in rows
            if (r.symbol, r.side, r.kind, r.level_price) not in live
        ]
        if stale:
            await session.execute(delete(PositionAlert).where(PositionAlert.id.in_(stale)))
            logger.info("Отметки приближения сняты: уровня больше нет",
                        extra={"user_id": user_id, "count": len(stale)})

    # --- проверка уровня (каждые 15 с) -------------------------------------

    async def _check(
        self, session: AsyncSession, snap: UserSnapshot, view: PositionView, kind: str,
        mark: Decimal,
    ) -> None:
        if not (snap.sl_on if kind == "SL" else snap.tp_on):
            return
        level = level_of(view, kind)
        if level is None:
            return
        p = view.position
        progress = progress_fraction(p.entry_price, level, mark)
        if progress is None:
            return
        threshold = Decimal(snap.sl_percent if kind == "SL" else snap.tp_percent) / HUNDRED
        existing = await session.scalar(select(PositionAlert).where(
            PositionAlert.user_id == snap.user_id, PositionAlert.symbol == p.symbol,
            PositionAlert.side == p.side, PositionAlert.kind == kind,
            PositionAlert.level_price == level,
        ))
        if existing is not None:
            if progress < threshold - HYSTERESIS:
                await session.delete(existing)
                await session.commit()
            return
        if progress < threshold:
            return
        precision = snap.precision.get(p.symbol)
        text = render_alert(view, kind, level, mark, snap.r_unit.get((p.symbol, p.side)),
                            precision)
        keyboard = alert_keyboard(
            view, mark, fee_rate=self._settings.exec_taker_fee_rate,
            min_distance_percent=self._settings.exec_min_stop_distance_percent,
            precision=precision,
        )
        delivery = await send_notification(self._bot, snap.telegram_id, text,
                                           reply_markup=keyboard)
        logger.info(
            "Уведомление о приближении",
            extra={
                "user_id": snap.user_id, "symbol": p.symbol, "side": p.side.value, "kind": kind,
                "level": str(level), "mark": str(mark),
                "passed_pct": str((progress * HUNDRED).quantize(Decimal("0.1"))),
                "overshoot_pp": str(((progress - threshold) * HUNDRED).quantize(Decimal("0.1"))),
                "delivery": delivery.value,
            },
        )
        # Отметка — только после окончательного исхода (28.09): при сбое
        # сети следующий цикл повторит, если цена ещё за порогом.
        if delivery.final:
            session.add(PositionAlert(
                user_id=snap.user_id, symbol=p.symbol, side=p.side, kind=kind,
                level_price=level, notified_at=datetime.now(UTC),
            ))
            await session.commit()
            self._stats["alerts"] += 1
