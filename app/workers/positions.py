"""Монитор приближения открытых позиций к TP/SL (этап 12).

В отличие от сканера сетапов, дедуп здесь не требует fingerprint и таблицы
signals: у открытой сделки ровно один TP и один SL, поэтому "не слать
повторно" сводится к двум таймстампам прямо на Trade
(tp_approach_notified_at/sl_approach_notified_at), которые сбрасываются в
None, если цена отъехала обратно — готовность к новому предупреждению при
повторном приближении.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from aiogram import Bot

from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models.trade import Trade
from app.database.repositories.trade import TradeRepository
from app.database.session import Database
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.workers.base import fmt_decimal
from app.workers.notifier import notification_enabled, send_notification

logger = get_logger(__name__)

ZERO = Decimal(0)
ONE = Decimal(1)


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


class PositionMonitor:
    def __init__(self, bot: Bot, db: Database, settings: Settings) -> None:
        self._bot = bot
        self._db = db
        self._settings = settings
        client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
        self._market = MarketDataService(client, TTLCache())

    async def run(self) -> None:
        async with self._db.session() as session:
            repo = TradeRepository(session)
            trades = await repo.list_all_open()
            if not trades:
                return

            symbols = sorted({t.symbol for t in trades})
            prices = await self._market.get_prices(symbols)

            for trade in trades:
                price = prices.get(trade.symbol)
                if price is None:
                    continue
                try:
                    await self._check_trade(trade, price)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Проверка позиции упала",
                        extra={"trade_id": trade.id, "symbol": trade.symbol},
                    )

            await session.flush()

    async def _check_trade(self, trade: Trade, price: Decimal) -> None:
        if not notification_enabled(trade.user.settings, "tp_sl_approaching"):
            return

        threshold = self._settings.position_monitor_approach_percent / Decimal(100)
        await self._check_target(
            trade, price, target=trade.take_profit, kind="tp", threshold=threshold
        )
        await self._check_target(
            trade, price, target=trade.stop_loss, kind="sl", threshold=threshold
        )

    async def _check_target(
        self,
        trade: Trade,
        price: Decimal,
        *,
        target: Decimal | None,
        kind: str,
        threshold: Decimal,
    ) -> None:
        if target is None:
            return

        progress = progress_fraction(trade.entry_price, target, price)
        if progress is None:
            return
        remaining = ONE - progress

        approaching = ZERO <= remaining <= threshold
        already_notified = getattr(trade, f"{kind}_approach_notified_at") is not None

        if approaching and not already_notified:
            setattr(trade, f"{kind}_approach_notified_at", datetime.now(UTC))
            label = "Take-Profit" if kind == "tp" else "Stop-Loss"
            icon = "🎯" if kind == "tp" else "🛑"
            text = (
                f"{icon} <b>{trade.symbol} приближается к {label}</b>\n\n"
                f"Текущая цена: {fmt_decimal(price)}\n"
                f"{label}: {fmt_decimal(target)}\n"
                f"Осталось: {fmt_decimal(remaining * 100)}% пути от входа"
            )
            await send_notification(self._bot, trade.user.telegram_id, text)
        elif not approaching and already_notified:
            # Цена отъехала обратно за порог — снимаем отметку, чтобы
            # повторное приближение снова дало уведомление.
            setattr(trade, f"{kind}_approach_notified_at", None)
