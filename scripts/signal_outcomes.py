"""Отчёт «исходы сигналов» — что стало с READY-уведомлениями по свечам.

Повторяемая версия разового retro_breakout (28.09). Только чтение: из БД —
SELECT в транзакции READ ONLY, с биржи — публичные свечи (GET klines),
ключи не нужны. Запуск на проде, после деплоя миграции ea93de72860d:

    docker compose exec -T bot python -m scripts.signal_outcomes [--json out.json]

Что считается:

- Исход: свечи ТФ сигнала после notified_at (только закрытые, открытые не
  раньше notified_at), горизонт HORIZON свечей. Первая свеча, задевшая
  стоп или тейк, решает исход; задела оба — стоп (консервативно, порядок
  внутри свечи неизвестен). Не задела за горизонт — «открыт».
- R: тейк — RR снимка |тейк − вход| / |вход − стоп|, стоп — −1. Вход —
  край зоны, дальний от стопа (signals.detector_entry), как у детектора.
  Открытые в средний R не входят — отдельной строкой.
- Нетто: R − комиссия в R, комиссия_R = rate × (вход + выход) / |вход − стоп|,
  rate — Settings.exec_taker_fee_rate, выход — цена тейка или стопа.
- Признаки: из снимка уведомления, если есть. Для строк до миграции —
  прогон детектора на свечах до notified_at: «replay», если стоп и тейк
  прогона совпали со снимком, иначе «replay≠». stop_pct у всех — из цен
  снимка, точно.
- Срез «один сигнал на пробой»: пробой с ретестом, ключ (символ, ТФ,
  направление, breakout_at), первое уведомление.

Лимиты BingX: пауза между запросами; остаток лимита по klines ≤ 2 — стоп
без отчёта (CLAUDE.md, «Замер лимитов — тоже нагрузка»).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.analysis.engine import CANDLES_REQUIRED, context_from_candles
from app.analysis.setups import BreakoutRetest, EMAPullback, SetupDetector
from app.analysis.signals import detector_entry, stop_percent
from app.exchanges.base import Kline
from app.trading.enums import SignalDirection

HORIZON = 50
TF_MINUTES = {"1h": 60, "4h": 240}
REQUEST_PAUSE_SECONDS = 0.3
RATE_LIMIT_STOP_REMAIN = 2

TAKE, STOP, OPEN = "тейк", "стоп", "открыт"
SNAPSHOT, REPLAY, REPLAY_MISMATCH = "снимок", "replay", "replay≠"

DETECTORS: dict[str, SetupDetector] = {
    BreakoutRetest.name: BreakoutRetest(),
    EMAPullback.name: EMAPullback(),
}

SELECT_READY = """
SELECT n.id, s.symbol, s.timeframe, n.setup, n.direction,
       n.entry_low, n.entry_high, n.stop_loss, n.take_profit, n.notified_at,
       n.atr, n.volume_ratio_last, n.stop_pct,
       n.breakout_volume_ratio, n.breakout_at, n.ema50_distance_atr
FROM signal_notifications n
JOIN signals s ON s.id = n.signal_id
WHERE n.level = 'READY'
ORDER BY n.notified_at, n.id
"""


# --- данные -----------------------------------------------------------------


@dataclass(slots=True)
class Features:
    atr: Decimal | None = None
    volume_ratio_last: Decimal | None = None
    stop_pct: Decimal | None = None
    breakout_volume_ratio: Decimal | None = None
    breakout_at: datetime | None = None
    ema50_distance_atr: Decimal | None = None

    @property
    def empty(self) -> bool:
        """Снимок до миграции: сканер признаков ещё не писал."""
        return self.atr is None and self.volume_ratio_last is None


@dataclass(slots=True)
class Row:
    id: int
    symbol: str
    timeframe: str
    setup: str
    direction: SignalDirection
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    notified_at: datetime
    features: Features = field(default_factory=Features)
    features_source: str = SNAPSHOT

    @property
    def entry(self) -> Decimal:
        entry = detector_entry(self.direction, self.entry_low, self.entry_high)
        assert entry is not None, f"уведомление {self.id}: нет входа"
        return entry

    @property
    def risk(self) -> Decimal:
        return abs(self.entry - self.stop_loss)

    @property
    def rr(self) -> Decimal:
        return abs(self.take_profit - self.entry) / self.risk


@dataclass(slots=True)
class Outcome:
    kind: str
    bar: int | None
    bars_seen: int
    entry_touched: bool


@dataclass(slots=True)
class Result:
    row: Row
    outcome: Outcome
    r_gross: Decimal | None
    r_net: Decimal | None


# --- чистая логика (тесты: tests/test_signal_outcomes.py) -------------------


def simulate(row: Row, candles: Sequence[Kline], horizon: int = HORIZON) -> Outcome:
    """Исход по свечам после уведомления — см. docstring модуля."""
    long_ = row.direction is SignalDirection.LONG
    entry = row.entry
    touched = False
    window = list(candles)[:horizon]
    for k, c in enumerate(window, 1):
        if (c.low <= entry) if long_ else (c.high >= entry):
            touched = True
        hit_stop = c.low <= row.stop_loss if long_ else c.high >= row.stop_loss
        hit_take = c.high >= row.take_profit if long_ else c.low <= row.take_profit
        if hit_stop:
            return Outcome(STOP, k, len(window), touched)
        if hit_take:
            return Outcome(TAKE, k, len(window), touched)
    return Outcome(OPEN, None, len(window), touched)


def fee_r(row: Row, exit_price: Decimal, rate: Decimal) -> Decimal:
    """Taker-комиссия входа и выхода в долях риска."""
    return rate * (row.entry + exit_price) / row.risk


def score(row: Row, outcome: Outcome, rate: Decimal) -> Result:
    if outcome.kind == TAKE:
        gross = row.rr
        net = gross - fee_r(row, row.take_profit, rate)
    elif outcome.kind == STOP:
        gross = Decimal(-1)
        net = gross - fee_r(row, row.stop_loss, rate)
    else:
        return Result(row, outcome, None, None)
    return Result(row, outcome, gross, net)


def forward_window(
    candles: Iterable[Kline], notified_at: datetime, now: datetime
) -> list[Kline]:
    """Закрытые свечи, открытые не раньше уведомления, по времени."""
    return sorted(
        (c for c in candles if c.open_time >= notified_at and c.close_time <= now),
        key=lambda c: c.open_time,
    )


def history_window(candles: Iterable[Kline], notified_at: datetime) -> list[Kline]:
    """Закрытые к моменту уведомления свечи — окно сканера без формирующейся:
    её OHLC на момент скана задним числом не восстановить."""
    closed = sorted(
        (c for c in candles if c.close_time <= notified_at), key=lambda c: c.open_time
    )
    return closed[-(CANDLES_REQUIRED - 1):]


def replay_features(row: Row, history: list[Kline]) -> tuple[Features, str]:
    """Признаки прогоном детектора на истории до уведомления."""
    features = Features(
        stop_pct=stop_percent(row.direction, row.entry_low, row.entry_high, row.stop_loss)
    )
    detector = DETECTORS.get(row.setup)
    if detector is None or len(history) < 50:
        return features, REPLAY_MISMATCH
    context = context_from_candles(row.symbol, row.timeframe, history)
    features.atr = context.atr
    features.volume_ratio_last = context.volume_ratio
    signal = detector.detect(context)
    match = (
        signal.is_actionable
        and signal.direction is row.direction
        and signal.stop_loss == row.stop_loss
        and signal.take_profit_1 == row.take_profit
    )
    if signal.is_actionable and signal.direction is row.direction:
        features.breakout_volume_ratio = signal.breakout_volume_ratio
        features.breakout_at = signal.breakout_at
        features.ema50_distance_atr = signal.ema50_distance_atr
    elif isinstance(detector, BreakoutRetest) and context.atr is not None:
        # Прогон не дал READY (последняя свеча скана не восстановима) —
        # пробой ищем тем же методом детектора, как retro_breakout.
        looking_long = row.direction is SignalDirection.LONG
        broken = detector._find_broken_level(context, looking_long, context.atr)
        if broken is not None:
            _, index = broken
            features.breakout_volume_ratio = detector._breakout_volume_ratio(context, index)
            features.breakout_at = history[index].open_time
    return features, REPLAY if match else REPLAY_MISMATCH


def one_per_breakout(results: Sequence[Result]) -> list[Result]:
    """Первое уведомление на каждый пробой; без breakout_at — вне среза."""
    seen: set[tuple[str, str, str, datetime]] = set()
    out: list[Result] = []
    for r in sorted(results, key=lambda r: (r.row.notified_at, r.row.id)):
        row = r.row
        if row.setup != BreakoutRetest.name or row.features.breakout_at is None:
            continue
        key = (row.symbol, row.timeframe, row.direction.value, row.features.breakout_at)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def bucket(value: Decimal | None, edges: Sequence[Decimal], unit: str = "") -> str:
    """«<a», «a–b», «≥b»; None — «н/д»."""
    if value is None:
        return "н/д"
    for i, edge in enumerate(edges):
        if value < edge:
            return f"<{edge}{unit}" if i == 0 else f"{edges[i - 1]}–{edge}{unit}"
    return f"≥{edges[-1]}{unit}"


def atr_pct(row: Row) -> Decimal | None:
    atr = row.features.atr
    return None if atr is None else atr / row.entry * 100


@dataclass(slots=True)
class Summary:
    label: str
    n: int
    takes: int
    stops: int
    open: int
    avg_r_gross: Decimal | None
    avg_r_net: Decimal | None
    sum_r_gross: Decimal
    replayed: int


def summarize(label: str, results: Sequence[Result]) -> Summary:
    closed = [r for r in results if r.r_gross is not None]
    gross = [r.r_gross for r in closed if r.r_gross is not None]
    net = [r.r_net for r in closed if r.r_net is not None]
    return Summary(
        label=label,
        n=len(results),
        takes=sum(1 for r in results if r.outcome.kind == TAKE),
        stops=sum(1 for r in results if r.outcome.kind == STOP),
        open=sum(1 for r in results if r.outcome.kind == OPEN),
        avg_r_gross=sum(gross, Decimal(0)) / len(gross) if gross else None,
        avg_r_net=sum(net, Decimal(0)) / len(net) if net else None,
        sum_r_gross=sum(gross, Decimal(0)),
        replayed=sum(1 for r in results if r.row.features_source != SNAPSHOT),
    )


def group(
    results: Sequence[Result], key: Callable[[Result], str]
) -> list[Summary]:
    groups: dict[str, list[Result]] = {}
    for r in results:
        groups.setdefault(key(r), []).append(r)
    return [summarize(k, v) for k, v in sorted(groups.items())]


VOLUME_EDGES = (Decimal("1.3"), Decimal("1.5"))
LAST_VOLUME_EDGES = (Decimal("1"), Decimal("1.5"))
STOP_PCT_EDGES = (Decimal("1"), Decimal("2"))
ATR_PCT_EDGES = (Decimal("0.5"), Decimal("1"))
EMA_DISTANCE_EDGES = (Decimal("0.25"),)


def slices(results: Sequence[Result]) -> dict[str, list[Summary]]:
    breakout = [r for r in results if r.row.setup == BreakoutRetest.name]
    first_per_breakout = one_per_breakout(results)
    pullback = [r for r in results if r.row.setup == EMAPullback.name]
    return {
        "Всего": [summarize("все", results)],
        "ТФ": group(results, lambda r: r.row.timeframe),
        "Сетап": group(results, lambda r: r.row.setup),
        "Направление": group(results, lambda r: r.row.direction.value),
        "ТФ × сетап": group(results, lambda r: f"{r.row.timeframe} · {r.row.setup}"),
        "Объём пробоя (пробой)": group(
            breakout, lambda r: bucket(r.row.features.breakout_volume_ratio, VOLUME_EDGES)
        ),
        "Объём последней свечи": group(
            results, lambda r: bucket(r.row.features.volume_ratio_last, LAST_VOLUME_EDGES)
        ),
        "Стоп, % от входа": group(
            results, lambda r: bucket(r.row.features.stop_pct, STOP_PCT_EDGES, "%")
        ),
        "ATR, % от входа": group(results, lambda r: bucket(atr_pct(r.row), ATR_PCT_EDGES, "%")),
        "Расстояние до EMA50, ATR (откат)": group(
            pullback, lambda r: bucket(r.row.features.ema50_distance_atr, EMA_DISTANCE_EDGES)
        ),
        "Один сигнал на пробой": [
            *group(first_per_breakout, lambda r: r.row.timeframe),
            summarize("все", first_per_breakout),
        ],
    }


def _fmt(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def render(
    results: Sequence[Result], rate: Decimal, horizon: int, skipped: Sequence[int] = ()
) -> str:
    replayed = [r for r in results if r.row.features_source != SNAPSHOT]
    matched = sum(1 for r in replayed if r.row.features_source == REPLAY)
    immature = sum(1 for r in results if r.outcome.kind == OPEN and r.outcome.bars_seen < horizon)
    touched = sum(1 for r in results if r.outcome.entry_touched)
    lines = [
        f"Исходы READY-сигналов: {len(results)} уведомлений, горизонт {horizon} свечей, "
        f"комиссия taker {rate} (нетто)",
        f"Признаки: из снимка {len(results) - len(replayed)}, восстановлено {len(replayed)}"
        + (
            f"; replay совпал со снимком {matched}/{len(replayed)} "
            f"({matched * 100 // len(replayed)}%)"
            if replayed
            else ""
        ),
        f"Открытые вне среднего R; из них горизонт ещё не пройден: {immature}. "
        f"Вход задет после уведомления: {touched}/{len(results)}",
    ]
    if skipped:
        lines.append(
            f"Пропущено READY без направления/цен/известного ТФ: {len(skipped)} "
            f"(id {', '.join(map(str, skipped[:20]))}{' …' if len(skipped) > 20 else ''})"
        )
    lines.append("")
    header = (
        f"{'срез':<34} {'n':>4} {'тейк':>5} {'стоп':>5} {'откр':>5} "
        f"{'R брутто':>9} {'R нетто':>9} {'ΣR брутто':>10} {'восст':>6}"
    )
    for title, rows in slices(results).items():
        lines += [f"== {title}", header]
        for s in rows:
            lines.append(
                f"{s.label[:34]:<34} {s.n:>4} {s.takes:>5} {s.stops:>5} {s.open:>5} "
                f"{_fmt(s.avg_r_gross):>9} {_fmt(s.avg_r_net):>9} "
                f"{_fmt(s.sum_r_gross):>10} {s.replayed:>6}"
            )
        lines.append("")
    return "\n".join(lines)


# --- ввод-вывод ---------------------------------------------------------------


class RateLimitStopError(RuntimeError):
    """Остаток лимита klines ≤ RATE_LIMIT_STOP_REMAIN — дальше не идём."""


def row_from_db(values: Sequence[Any]) -> Row | None:
    """None — у READY нет направления или цен: считать исход не от чего.
    Такие строки не молча выпадают — render() печатает их число."""
    (
        nid, symbol, timeframe, setup, direction, entry_low, entry_high, stop, take,
        notified_at, atr, vr_last, stop_pct, br_vr, br_at, ema_dist,
    ) = values
    if direction not in (SignalDirection.LONG.value, SignalDirection.SHORT.value) or None in (
        entry_low, entry_high, stop, take
    ) or timeframe not in TF_MINUTES:
        return None
    return Row(
        id=nid, symbol=symbol, timeframe=timeframe, setup=setup,
        direction=SignalDirection(direction),
        entry_low=entry_low, entry_high=entry_high, stop_loss=stop, take_profit=take,
        notified_at=notified_at,
        features=Features(atr, vr_last, stop_pct, br_vr, br_at, ema_dist),
    )


async def load_rows() -> tuple[list[Row], list[int]]:
    """SELECT в транзакции READ ONLY — запись отказала бы на стороне БД.
    Возвращает строки и id пропущенных (см. row_from_db)."""
    from sqlalchemy import text

    from app.core.config import get_settings
    from app.database.session import Database

    db = Database(get_settings())
    try:
        async with db.engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            rows = (await conn.execute(text(SELECT_READY))).all()
            await conn.rollback()
    finally:
        await db.dispose()
    parsed = [(r[0], row_from_db(r)) for r in rows]
    return [r for _, r in parsed if r is not None], [nid for nid, r in parsed if r is None]


class KlineFetcher:
    """Публичные свечи с паузой и стопом по остатку лимита."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.requests = 0

    async def get(self, symbol: str, timeframe: str, limit: int, end_time: datetime) -> list[Kline]:
        from app.exchanges.bingx import QUOTE_KLINES

        candles: list[Kline] = await self._client.get_klines(
            symbol, timeframe, limit=limit, end_time=end_time
        )
        self.requests += 1
        state = getattr(self._client, "_rate_limits", {}).get(("GET", QUOTE_KLINES))
        if state is not None and state.remaining <= RATE_LIMIT_STOP_REMAIN:
            raise RateLimitStopError(
                f"остаток лимита klines {state.remaining} после {self.requests} запросов"
            )
        await asyncio.sleep(REQUEST_PAUSE_SECONDS)
        return candles


async def evaluate(
    rows: Sequence[Row], fetcher: KlineFetcher, rate: Decimal, horizon: int, now: datetime
) -> list[Result]:
    results: list[Result] = []
    for row in rows:
        step = timedelta(minutes=TF_MINUTES[row.timeframe])
        if row.features.empty:
            history = history_window(
                await fetcher.get(row.symbol, row.timeframe, CANDLES_REQUIRED, row.notified_at),
                row.notified_at,
            )
            row.features, row.features_source = replay_features(row, history)
        elif row.features.stop_pct is None:
            row.features.stop_pct = stop_percent(
                row.direction, row.entry_low, row.entry_high, row.stop_loss
            )
        forward = forward_window(
            await fetcher.get(
                row.symbol, row.timeframe, horizon + 5,
                min(row.notified_at + step * (horizon + 2), now),
            ),
            row.notified_at,
            now,
        )
        results.append(score(row, simulate(row, forward, horizon), rate))
    return results


def _json_default(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--horizon", type=int, default=HORIZON)
    parser.add_argument("--json", dest="json_path", help="сырые строки в JSON-файл")
    args = parser.parse_args(argv)

    from app.core.config import get_settings
    from app.services.exchange_factory import ExchangeFactory

    settings = get_settings()
    rate = settings.exec_taker_fee_rate
    rows, skipped = await load_rows()
    client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
    try:
        results = await evaluate(
            rows, KlineFetcher(client), rate, args.horizon, datetime.now(UTC)
        )
    except RateLimitStopError as exc:
        print(f"СТОП по лимиту BingX: {exc}. Отчёт не построен.", file=sys.stderr)
        return 2
    finally:
        await client.close()

    print(render(results, rate, args.horizon, skipped))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        **{k: v for k, v in asdict(r.row).items() if k != "features"},
                        **asdict(r.row.features),
                        "outcome": r.outcome.kind,
                        "bar": r.outcome.bar,
                        "bars_seen": r.outcome.bars_seen,
                        "entry_touched": r.outcome.entry_touched,
                        "rr": r.row.rr,
                        "r_gross": r.r_gross,
                        "r_net": r.r_net,
                    }
                    for r in results
                ],
                fh, ensure_ascii=False, indent=1, default=_json_default,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
