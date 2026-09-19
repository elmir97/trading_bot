"""Импорт истории сделок с биржи.

Биржа отдаёт поток исполнений, а журналу нужны сделки. Задача сервиса —
собрать одно из другого, не создав дублей и не склеив разные позиции.

Правило сборки: исполнения одного символа и направления накапливаются,
пока объём позиции не вернётся к нулю. Момент обнуления закрывает сделку;
следующее исполнение начинает новую. Так один и тот же инструмент,
торгуемый несколько раз за день, не превращается в одну гигантскую сделку.

Дедупликация идёт по external_id исполнения: окна запросов к бирже
намеренно перекрываются, чтобы не терять данные на границах, и без
проверки повторов история задваивалась бы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.logging import get_logger
from app.database.models.trade import Trade, TradeFill
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import ExchangeClient, Fill
from app.trading.calculations import (
    FillData,
    aggregate_fills,
    calculate_pnl,
    calculate_pnl_percent_of_balance,
)
from app.trading.enums import FillSide, TradeSide, TradeSource, TradeStatus

logger = get_logger(__name__)

ZERO = Decimal(0)

# Окно одного запроса к бирже. BingX ограничивает диапазон истории,
# поэтому длинный период режется на отрезки.
WINDOW = timedelta(days=7)

# Перекрытие соседних окон: исполнение на самой границе иначе может
# не попасть ни в одно из них.
OVERLAP = timedelta(minutes=5)


@dataclass(slots=True)
class ImportResult:
    fills_received: int = 0
    fills_new: int = 0
    trades_created: int = 0
    trades_updated: int = 0
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        if self.errors and not self.fills_new:
            return "Импорт не удался:\n" + "\n".join(f"• {e}" for e in self.errors)

        lines = [
            f"Получено исполнений: {self.fills_received}",
            f"Новых: {self.fills_new}",
            f"Создано сделок: {self.trades_created}",
        ]
        if self.trades_updated:
            lines.append(f"Дополнено сделок: {self.trades_updated}")
        if self.errors:
            lines.append(f"\nЧастичные ошибки: {len(self.errors)}")
        return "\n".join(lines)


@dataclass(slots=True)
class _PendingTrade:
    """Накопитель исполнений одной позиции до её закрытия."""

    symbol: str
    side: TradeSide
    fills: list[Fill] = field(default_factory=list)

    @property
    def open_quantity(self) -> Decimal:
        opened = sum((f.quantity for f in self.fills if f.is_entry), ZERO)
        closed = sum((f.quantity for f in self.fills if not f.is_entry), ZERO)
        return opened - closed

    @property
    def is_closed(self) -> bool:
        return self.open_quantity <= ZERO and any(f.is_entry for f in self.fills)


def group_fills_into_trades(fills: list[Fill]) -> list[_PendingTrade]:
    """Собирает поток исполнений в отдельные сделки.

    Ключ группировки — символ и сторона позиции. Лонг и шорт по одному
    инструменту могут быть открыты одновременно (режим хеджирования),
    и смешивать их нельзя.
    """
    ordered = sorted(fills, key=lambda f: f.executed_at)
    active: dict[tuple[str, TradeSide], _PendingTrade] = {}
    finished: list[_PendingTrade] = []

    for fill in ordered:
        key = (fill.symbol, fill.side)
        pending = active.get(key)

        if pending is None:
            if not fill.is_entry:
                # Выход без входа: позиция открыта до начала импорта.
                # Пропускаем — восстановить её цену входа нечем, а
                # придуманная сделка исказит статистику.
                logger.info(
                    "Пропущен выход без входа",
                    extra={"symbol": fill.symbol, "fill_id": fill.external_id},
                )
                continue
            pending = _PendingTrade(symbol=fill.symbol, side=fill.side)
            active[key] = pending

        pending.fills.append(fill)

        if pending.is_closed:
            finished.append(pending)
            del active[key]

    # Незакрытые позиции тоже попадают в журнал — как открытые сделки.
    finished.extend(active.values())
    return finished


class HistoryImporter:
    def __init__(
        self,
        client: ExchangeClient,
        trades: TradeRepository,
        user_id: int,
    ) -> None:
        self._client = client
        self._trades = trades
        self._user_id = user_id

    async def fetch_fills(
        self, start: datetime, end: datetime
    ) -> tuple[list[Fill], list[str]]:
        """Тянет исполнения за период окнами с перекрытием."""
        collected: dict[str, Fill] = {}
        errors: list[str] = []

        window_start = start
        while window_start < end:
            window_end = min(window_start + WINDOW, end)
            try:
                batch = await self._client.get_fills(window_start, window_end)
                for fill in batch:
                    if fill.external_id:
                        collected[fill.external_id] = fill
            except Exception as exc:
                # ронять весь импорт: остальные окна могут пройти успешно.
                logger.warning(
                    "Окно импорта не загрузилось",
                    extra={"start": window_start.isoformat(), "error": str(exc)},
                )
                errors.append(
                    f"{window_start:%d.%m.%Y} — {window_end:%d.%m.%Y}: {exc}"
                )

            # Выход по достижении конца периода, а не по значению
            # следующего окна: сдвиг на перекрытие может оказаться
            # меньше нуля, и цикл никогда бы не завершился.
            if window_end >= end:
                break
            window_start = window_end - OVERLAP

        return list(collected.values()), errors

    async def import_period(
        self, start: datetime, end: datetime, account_balance: Decimal | None = None
    ) -> ImportResult:
        result = ImportResult()

        fills, errors = await self.fetch_fills(start, end)
        result.fills_received = len(fills)
        result.errors = errors

        if not fills:
            return result

        # Отсекаем уже импортированное одним запросом: за год исполнений
        # могут быть тысячи, и проверять их по одному недопустимо.
        known = await self._trades.existing_fill_ids(
            self._user_id, self._client.name, [f.external_id for f in fills]
        )
        fresh = [f for f in fills if f.external_id not in known]
        result.fills_new = len(fresh)

        if not fresh:
            return result

        for pending in group_fills_into_trades(fresh):
            trade = self._build_trade(pending, account_balance)
            self._trades.add(trade)
            result.trades_created += 1

        await self._trades.flush()
        logger.info(
            "Импорт завершён",
            extra={
                "user_id": self._user_id,
                "fills": result.fills_new,
                "trades": result.trades_created,
            },
        )
        return result

    def _build_trade(
        self, pending: _PendingTrade, account_balance: Decimal | None
    ) -> Trade:
        """Собирает сделку журнала из накопленных исполнений."""
        aggregate = aggregate_fills(
            FillData(
                fill_side=FillSide.ENTRY if f.is_entry else FillSide.EXIT,
                price=f.price,
                quantity=f.quantity,
                fee=f.fee,
            )
            for f in pending.fills
        )

        entries = [f for f in pending.fills if f.is_entry]
        exits = [f for f in pending.fills if not f.is_entry]
        opened_at = min(f.executed_at for f in entries)
        closed = bool(exits) and aggregate.open_quantity <= ZERO

        trade = Trade(
            user_id=self._user_id,
            exchange=self._client.name,
            symbol=pending.symbol,
            side=pending.side,
            entry_price=aggregate.entry_price,
            exit_price=aggregate.exit_price,
            quantity=aggregate.entry_quantity,
            fees=aggregate.total_fees,
            status=TradeStatus.CLOSED if closed else TradeStatus.OPEN,
            source=TradeSource.IMPORTED,
            # Импортированная сделка не размечена: биржа не знает ни
            # стратегии, ни причины входа. До разметки она не участвует
            # в срезах по стратегиям и ошибкам.
            is_annotated=False,
            account_balance_at_entry=account_balance,
            opened_at=opened_at,
            closed_at=max(f.executed_at for f in exits) if closed else None,
            external_position_id=pending.fills[0].position_id,
            fills=[
                TradeFill(
                    user_id=self._user_id,
                    exchange=self._client.name,
                    fill_side=FillSide.ENTRY if f.is_entry else FillSide.EXIT,
                    price=f.price,
                    quantity=f.quantity,
                    fee=f.fee,
                    executed_at=f.executed_at,
                    external_fill_id=f.external_id,
                )
                for f in pending.fills
            ],
            mistakes=[],
        )

        if closed and aggregate.entry_price and aggregate.exit_price:
            pnl = calculate_pnl(
                entry_price=aggregate.entry_price,
                exit_price=aggregate.exit_price,
                quantity=aggregate.exit_quantity,
                side=pending.side,
                fees=aggregate.total_fees,
            )
            trade.pnl = pnl
            if account_balance and account_balance > ZERO:
                trade.pnl_percent = calculate_pnl_percent_of_balance(
                    pnl=pnl, account_balance=account_balance
                )

        return trade


def default_import_range(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    return end - timedelta(days=days), end
