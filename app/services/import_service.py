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
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import ExchangeClient, ExchangeError, Fill, Position
from app.trading.calculations import (
    FillData,
    aggregate_fills,
    calculate_pnl,
    calculate_pnl_percent_of_balance,
)
from app.trading.enums import ExchangeKeyMode, FillSide, TradeSide, TradeSource, TradeStatus

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
    # Шаг 15.5.4: исполнения ордеров бота — их сделка уже в журнале.
    fills_skipped_bot: int = 0
    trades_created: int = 0
    trades_updated: int = 0
    errors: list[str] = field(default_factory=list)
    # M4: отсечка журнала. Окна раньше неё не запрашиваются, поэтому число
    # пропущенных исполнений не известно; fills_before_cutoff — только
    # отброшенные на границе (перекрытие окон).
    cutoff: datetime | None = None
    fills_before_cutoff: int = 0
    all_before_cutoff: bool = False

    def render(self, tz_offset: int = 0) -> str:
        cutoff_line = (
            f"Журнал ведётся с {fmt_cutoff(self.cutoff, tz_offset)} — исполнения раньше "
            "не импортируются."
            if self.cutoff is not None else None
        )
        if self.all_before_cutoff and self.cutoff is not None:
            return (
                f"Весь период раньше начала журнала ({fmt_cutoff(self.cutoff, tz_offset)}) — "
                "импортировать нечего."
            )
        if self.errors and not self.fills_new:
            lines = ["Импорт не удался:", *(f"• {e}" for e in self.errors)]
            if cutoff_line:
                lines += ["", cutoff_line]
            return "\n".join(lines)

        lines = [
            f"Получено исполнений: {self.fills_received}",
            f"Новых: {self.fills_new}",
            f"Создано сделок: {self.trades_created}",
        ]
        if self.fills_skipped_bot:
            lines.append(f"Пропущено исполнений ордеров бота: {self.fills_skipped_bot}")
        if self.trades_updated:
            lines.append(f"Дополнено сделок: {self.trades_updated}")
        if self.errors:
            lines.append(f"\nЧастичные ошибки: {len(self.errors)}")
        if cutoff_line:
            lines += ["", cutoff_line]
        return "\n".join(lines)


def fmt_cutoff(cutoff: datetime, tz_offset: int = 0) -> str:
    """Отсечка журнала в местном времени пользователя с явной меткой пояса:
    «03.10.2026 23:38 (UTC+5)»."""
    local = cutoff + timedelta(hours=tz_offset)
    zone = f"UTC{tz_offset:+d}" if tz_offset else "UTC"
    return f"{local:%d.%m.%Y %H:%M} ({zone})"


@dataclass(frozen=True, slots=True)
class OpenPositionImport:
    """Итог «📥 В журнал»: сделка или причина отказа (ничего не записано)."""

    trade: Trade | None
    refusal: str | None


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
        *,
        account_mode: ExchangeKeyMode,
        journal_cutoff: datetime | None = None,
        tz_offset: int = 0,
    ) -> None:
        """account_mode — счёт клиента (DEMO/LIVE): пишется в каждую сделку
        импорта, лимиты убытка считаются по нему (03.10.2026). Обязателен —
        счёт по умолчанию был бы молчаливой подстановкой.

        journal_cutoff — user_settings.journal_cutoff_at (M4): исполнения
        раньше него в журнал не заводятся. None — отсечки нет."""
        self._client = client
        self._trades = trades
        self._user_id = user_id
        self._account_mode = account_mode
        self._cutoff = journal_cutoff
        self._tz_offset = tz_offset

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
                    # Дедуп только по непустому ключу: без него исполнение
                    # не отличить от уже импортированного (WARNING — в разборе).
                    if fill.external_id is not None:
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
        self,
        start: datetime,
        end: datetime,
        account_balance: Decimal | None = None,
    ) -> ImportResult:
        result = ImportResult(cutoff=self._cutoff)
        if self._cutoff is not None:
            if end <= self._cutoff:
                # Весь период раньше начала журнала — к бирже не ходим.
                result.all_before_cutoff = True
                return result
            # Окна раньше отсечки не запрашиваются вовсе.
            start = max(start, self._cutoff)

        fills, errors = await self.fetch_fills(start, end)
        fills = self._drop_before_cutoff(fills, result)
        result.fills_received = len(fills)
        result.errors = errors

        if not fills:
            return result

        # Отсекаем уже импортированное одним запросом: за год исполнений
        # могут быть тысячи, и проверять их по одному недопустимо.
        known = await self._trades.existing_fill_ids(
            self._user_id, self._client.name,
            [f.external_id for f in fills if f.external_id is not None],
        )
        fresh = [f for f in fills if f.external_id not in known]
        fresh = await self._drop_bot_fills(fresh, result)
        result.fills_new = len(fresh)

        if not fresh:
            return result

        groups = group_fills_into_trades(fresh)
        live = await self._live_positions() if any(not g.is_closed for g in groups) else []
        for pending in groups:
            trade = self._build_trade(pending, account_balance)
            if not pending.is_closed:
                # 03.10: незакрытой сделке — positionId живой позиции того же
                # символа и стороны с тем же объёмом (в истории исполнений
                # BingX его нет): reconciler закроет сделку, даже если позиция
                # закроется до его первого цикла.
                position = next(
                    (p for p in live
                     if p.symbol == pending.symbol and p.side is pending.side
                     and p.quantity == pending.open_quantity),
                    None,
                )
                if position is not None and position.position_id:
                    trade.external_position_id = position.position_id
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

    def _drop_before_cutoff(self, fills: list[Fill], result: ImportResult) -> list[Fill]:
        """Окна идут с перекрытием, первое начинается на самой отсечке —
        исполнения раньше неё отбрасываются поштучно и считаются в ответе."""
        if self._cutoff is None:
            return fills
        kept = [f for f in fills if f.executed_at >= self._cutoff]
        result.fills_before_cutoff += len(fills) - len(kept)
        return kept

    async def _live_positions(self) -> list[Position]:
        try:
            return list(await self._client.get_positions() or [])
        except ExchangeError:
            logger.warning("Импорт: позиции биржи не получены — positionId не записан",
                           extra={"user_id": self._user_id})
            return []

    async def import_open_position(
        self, start: datetime, end: datetime, *, symbol: str, side: TradeSide, quantity: Decimal,
        position_id: str | None = None, account_balance: Decimal | None = None,
    ) -> OpenPositionImport:
        """Кнопка «📥 В журнал» (экран «Позиции»): только ТЕКУЩАЯ открытая
        позиция — исполнения её входа (и частичных выходов, если были).
        Закрытые сделки символа за период не создаются: история — /import.

        Исполнения символа и стороны группируются как в обычном импорте;
        текущая позиция — незакрытая группа. Её открытый объём обязан
        совпасть с объёмом позиции на бирже, иначе отказ без записи: вход
        старше периода или исполнения не все — сделка с чужой ценой входа
        испортила бы статистику."""
        fills, errors = await self.fetch_fills(start, end)
        if errors:
            return OpenPositionImport(None, "Биржа отдала историю не полностью: " + errors[0])
        fills = [f for f in fills if f.symbol == symbol and f.side == side]
        active = [g for g in group_fills_into_trades(fills) if not g.is_closed]
        if not active:
            return OpenPositionImport(None, "Исполнений входа текущей позиции не найдено.")
        pending = active[-1]
        if self._cutoff is not None and any(
            f.executed_at < self._cutoff for f in pending.fills
        ):
            # Позиция открыта до очистки журнала: её вход — «до начала
            # журнала», заносить половину позиции нельзя, а всю — значит
            # вернуть в журнал удалённое.
            return OpenPositionImport(
                None,
                "Позиция открыта до начала журнала "
                f"({fmt_cutoff(self._cutoff, self._tz_offset)}) — в журнал не заносится.",
            )
        if pending.open_quantity != quantity:
            return OpenPositionImport(
                None,
                f"Объём по исполнениям ({pending.open_quantity.normalize():f}) не сходится "
                f"с позицией на бирже ({quantity.normalize():f}) — вход, видимо, раньше "
                "периода. Сделку не создал.",
            )
        ids = [f.external_id for f in pending.fills if f.external_id is not None]
        known = await self._trades.existing_fill_ids(self._user_id, self._client.name, ids)
        result = ImportResult()
        kept = await self._drop_bot_fills(pending.fills, result)
        if known or len(kept) != len(pending.fills):
            return OpenPositionImport(None, "Исполнения этой позиции уже есть в журнале.")
        trade = self._build_trade(pending, account_balance)
        # positionId позиции с биржи (в истории исполнений BingX его нет):
        # связь для reconciler, когда позиция закроется (02.10, #12: без неё
        # сделка осталась OPEN после стопа).
        if position_id:
            trade.external_position_id = position_id
        self._trades.add(trade)
        await self._trades.flush()
        logger.info(
            "Позиция занесена в журнал",
            extra={"user_id": self._user_id, "symbol": symbol, "fills": len(pending.fills)},
        )
        return OpenPositionImport(trade, None)

    async def _drop_bot_fills(self, fills: list[Fill], result: ImportResult) -> list[Fill]:
        """Шаг 15.5.4: исполнения ордеров бота (вход, стоп, тейк) не
        импортируются — сделка бота уже в журнале с notification_id, а у её
        TradeFill нет tradeId биржи, и обычный дедуп по external_fill_id её
        не узнал бы. Выход, закрывший позицию бота руками, остаётся
        «выходом без входа» и пропускается group_fills_into_trades —
        половинчатой сделки нет.

        Исполнение без orderId отличить от ордеров бота нельзя — оно
        импортируется как раньше, но не тихо: warning о возможном дубле."""
        # Три признака исполнения бота: orderId — его вход/условник
        # (execution_orders); triggerOrderId — дочерний ордер сработавшего
        # стопа/тейка бота (свой orderId, снято живьём 27.09); orderId среди
        # исполнений сделок бота — выход, уже записанный reconciler'ом.
        bot_order_ids = await ExecutionOrderRepository(self._trades.session).exchange_order_ids(
            self._user_id
        )
        bot_fill_ids = await self._trades.bot_fill_external_ids(self._user_id)
        kept: list[Fill] = []
        for f in fills:
            if f.order_id is None:
                logger.warning(
                    "Исполнение без orderId — не могу отличить от ордеров бота, "
                    "возможен дубль сделки бота в журнале",
                    extra={"user_id": self._user_id, "fill_id": f.external_id, "symbol": f.symbol},
                )
                kept.append(f)
            elif (
                f.order_id in bot_order_ids
                or f.order_id in bot_fill_ids
                or (f.trigger_order_id is not None and f.trigger_order_id in bot_order_ids)
            ):
                result.fills_skipped_bot += 1
            else:
                kept.append(f)
        return kept

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
            account_mode=self._account_mode,
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
