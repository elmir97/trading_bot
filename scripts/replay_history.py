"""Replay детекторов на истории — исходы READY за 12 месяцев, проверка фильтров на половинах.

Детекторы не трогаем: на закрытии каждой свечи — context_from_candles и
AnalysisEngine.evaluate (тот же выбор лучшего сигнала, что у сканера),
classify_signal и эмуляция слотов сканера (scanner._handle_signal: READY
уведомляет, если слот новый, отпечаток сменился или истёк TTL; FORMING и
«нет сетапа» гасят READY-слот). Исход — simulate/score из signal_outcomes:
горизонт 50 свечей, обе цели в одной свече — стоп, taker-комиссия.

Запуск локально (публичные ручки BingX, без ключей, прод не участвует):

    python -m scripts.replay_history calibrate --live so.json
    python -m scripts.replay_history run --months 12 [--json out.json]

calibrate — сверка с живыми READY (JSON из signal_outcomes --json) за их
окно: живое уведомление найдено, если replay дал READY с тем же символом,
ТФ, направлением, стопом и тейком в окне [−3; +1] свечи. Меньше 80% —
exit 3, основной прогон не запускать.

run — 12 месяцев по H1 и H4. Символ без полной истории (первая свеча позже
начала окна с прогревом) исключается, это печатается. Отчёт по каждой
половине окна: база без фильтров и срезы, R брутто / нетто / нетто с
funding. Funding — сумма исторических ставок × markPrice за удержание (от
уведомления до свечи выхода), в долях риска; нет истории funding на весь
срок — «н/д», не оценка.

Кэш свечей и funding — gzip-JSONL в data/replay/ (в .gitignore). Лимит
BingX: пауза между запросами, остаток ≤ 2 — стоп (KlineFetcher).

Известные искажения: живой сканер раз в 30 минут видит и формирующуюся
свечу, replay — только закрытые; параметры детекторов — сегодняшние;
проскальзывание не учтено.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import random
import sys
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.analysis.classify import classify_signal
from app.analysis.engine import CANDLES_REQUIRED, AnalysisEngine, context_from_candles
from app.analysis.false_breakout import FalseBreakout
from app.analysis.indicators import ema, last_value
from app.analysis.setups import BreakoutRetest
from app.analysis.signals import MarketContext, Signal, stop_percent
from app.core.config import Settings
from app.exchanges.base import Kline
from app.trading.enums import SignalDirection, SignalLevel
from app.workers.scanner import build_fingerprint
from scripts.signal_outcomes import (
    HORIZON,
    OPEN,
    STOP,
    TAKE,
    Features,
    KlineFetcher,
    RateLimitStopError,
    Result,
    Row,
    bucket,
    forward_window,
    score,
    simulate,
)

TF_MINUTES = {"1h": 60, "4h": 240, "1d": 1440}
REPLAY_TIMEFRAMES = ("1h", "4h")
KLINE_LIMIT = 1000
FUNDING_PATH = "/openApi/swap/v2/quote/fundingRate"
FUNDING_LIMIT = 1000
NULL_RETRIES = 3
CACHE_DIR = Path("data/replay")
# EXEC_SYMBOL_WHITELIST прода (docker-compose.override.yml, 01.10).
DEFAULT_SYMBOLS = (
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "GRAMTON-USDT",
)
CALIBRATION_THRESHOLD = Decimal("0.8")
MATCH_BARS_BEFORE = 3
MATCH_BARS_AFTER = 1
D1_EMA_PERIOD = 200
# Значения по умолчанию из Settings — без чтения окружения: скрипт локальный.
TTL = timedelta(hours=Settings.model_fields["setup_scanner_ttl_hours"].default)
FEE_RATE: Decimal = Settings.model_fields["exec_taker_fee_rate"].default


def step(tf: str) -> timedelta:
    return timedelta(minutes=TF_MINUTES[tf])


# --- кэш ---------------------------------------------------------------------


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC)


def save_klines(path: Path, candles: Sequence[Kline]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for c in candles:
            fh.write(json.dumps({
                "t": _ms(c.open_time), "o": str(c.open), "h": str(c.high),
                "l": str(c.low), "c": str(c.close), "v": str(c.volume),
            }) + "\n")


def load_klines(path: Path, tf: str) -> list[Kline]:
    out: list[Kline] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            t = _from_ms(d["t"])
            out.append(Kline(
                open_time=t, open=Decimal(d["o"]), high=Decimal(d["h"]), low=Decimal(d["l"]),
                close=Decimal(d["c"]), volume=Decimal(d["v"]), close_time=t + step(tf),
            ))
    return out


@dataclass(frozen=True, slots=True)
class FundingEvent:
    time: datetime
    rate: Decimal
    mark_price: Decimal


def save_funding(path: Path, events: Sequence[FundingEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(
                {"t": _ms(e.time), "r": str(e.rate), "m": str(e.mark_price)}
            ) + "\n")


def load_funding(path: Path) -> list[FundingEvent]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [
            FundingEvent(_from_ms(d["t"]), Decimal(d["r"]), Decimal(d["m"]))
            for d in (json.loads(line) for line in fh)
        ]


# --- загрузка с биржи ---------------------------------------------------------


async def fetch_klines(
    fetcher: Any, symbol: str, tf: str, start: datetime, end: datetime
) -> list[Kline]:
    """Назад от end страницами по KLINE_LIMIT, пока не дойдём до start или
    биржа не перестанет отдавать старше. Только закрытые к end, по времени."""
    collected: dict[datetime, Kline] = {}
    cursor = end
    while True:
        batch = await fetcher.get(symbol, tf, KLINE_LIMIT, cursor)
        fresh = [c for c in batch if c.open_time not in collected]
        for c in batch:
            collected[c.open_time] = c
        if not fresh:
            break
        earliest = min(c.open_time for c in batch)
        if earliest <= start:
            break
        cursor = earliest - timedelta(milliseconds=1)
    return sorted(
        (c for c in collected.values() if c.open_time >= start and c.close_time <= end),
        key=lambda c: c.open_time,
    )


def parse_funding(data: Any) -> list[FundingEvent]:
    """Живая форма 01.10: список {symbol, fundingRate, fundingTime, markPrice}.
    Нет поля — ошибка, не ноль."""
    if not isinstance(data, list):
        raise ValueError(
            f"fundingRate: ожидался список, пришёл {type(data).__name__}: {data!r:.200}"
        )
    events: list[FundingEvent] = []
    for item in data:
        try:
            events.append(FundingEvent(
                _from_ms(int(item["fundingTime"])),
                Decimal(str(item["fundingRate"])),
                Decimal(str(item["markPrice"])),
            ))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError(f"fundingRate: битая запись {item!r}") from exc
    return events


class FundingFetcher:
    """Публичная история funding с паузой и стопом по остатку лимита."""

    def __init__(self, client: Any, pause: float = 0.3, null_pause: float = 2.0) -> None:
        self._client = client
        self._pause = pause
        self._null_pause = null_pause
        self.requests = 0
        self.null_retries = 0

    async def get(
        self, symbol: str, end: datetime | None, *, null_is_end: bool = False
    ) -> list[FundingEvent]:
        """end None — последние FUNDING_LIMIT записей. 01.10: endTime в текущем
        периоде funding (сразу после начисления) ручка стабильно отвечает
        data: null; без endTime и с endTime в прошлом — список."""
        params: dict[str, Any] = {"symbol": symbol, "limit": FUNDING_LIMIT}
        if end is not None:
            params["endTime"] = _ms(end)
        # 01.10: ручка изредка отвечает code 0 и data: null на тот же запрос,
        # что через секунду отдаёт список. Пустота — не «ставок нет»: повтор
        # до NULL_RETRIES раз, потом parse_funding падает, как на любом
        # не-списке.
        for attempt in range(NULL_RETRIES + 1):
            data = await self._client._request(FUNDING_PATH, params)
            self.requests += 1
            if data is not None or attempt == NULL_RETRIES:
                break
            self.null_retries += 1
            print(f"fundingRate {symbol}: data null, повтор {attempt + 1}", file=sys.stderr)
            await asyncio.sleep(self._null_pause)
        state = getattr(self._client, "_rate_limits", {}).get(("GET", FUNDING_PATH))
        if state is not None and state.remaining <= 2:
            raise RateLimitStopError(
                f"остаток лимита fundingRate {state.remaining} после {self.requests} запросов"
            )
        await asyncio.sleep(self._pause)
        if data is None and null_is_end:
            # 01.10: на endTime раньше начала истории символа (GRAMTON — с 2026)
            # ручка тоже отвечает null. Для старой страницы это конец истории;
            # покрытие проверит funding_gap, оценки нет.
            return []
        return parse_funding(data)


async def fetch_funding(
    fetcher: Any, symbol: str, start: datetime, end: datetime
) -> list[FundingEvent]:
    """Назад по endTime. Биржа перестала отдавать старше (endTime не
    сдвигает выдачу или история кончилась) — останавливаемся: покрытие
    начинается с самого раннего полученного события, раньше — «н/д»."""
    collected: dict[datetime, FundingEvent] = {}
    cursor: datetime | None = None  # первая страница — без endTime, см. FundingFetcher.get
    while True:
        batch = await fetcher.get(symbol, cursor, null_is_end=cursor is not None)
        fresh = [e for e in batch if e.time not in collected]
        for e in batch:
            collected[e.time] = e
        if not fresh:
            break
        earliest = min(e.time for e in batch)
        if earliest <= start:
            break
        cursor = earliest - timedelta(milliseconds=1)
    return sorted((e for e in collected.values() if e.time <= end), key=lambda e: e.time)


# --- replay: слоты сканера ----------------------------------------------------


@dataclass(slots=True)
class SlotState:
    """READY-слот одной пары (символ, ТФ): активен ли, последнее отправленное."""

    active: bool = False
    last_fingerprint: str | None = None
    last_expires: datetime | None = None


def slot_step(
    state: SlotState, level: SignalLevel | None, fingerprint: str | None,
    now: datetime, ttl: timedelta = TTL,
) -> bool:
    """Как scanner._handle_signal для READY: True — ушло бы уведомление."""
    if level is not SignalLevel.READY:
        state.active = False
        return False
    notify = (
        not state.active
        or state.last_fingerprint != fingerprint
        or (state.last_expires is not None and now >= state.last_expires)
    )
    state.active = True
    if notify:
        state.last_fingerprint = fingerprint
        state.last_expires = now + ttl
    return notify


_ENGINE = AnalysisEngine(None)  # type: ignore[arg-type]  # evaluate() рынка не трогает


def engine_evaluate(context: MarketContext) -> Signal:
    return _ENGINE.evaluate(context)


def row_from_signal(
    nid: int, signal: Signal, context: MarketContext, symbol: str, tf: str, at: datetime
) -> Row:
    assert signal.direction is not None
    assert signal.entry_zone_low is not None and signal.entry_zone_high is not None
    assert signal.stop_loss is not None and signal.take_profit_1 is not None
    return Row(
        id=nid, symbol=symbol, timeframe=tf, setup=signal.setup,
        direction=signal.direction,
        entry_low=signal.entry_zone_low, entry_high=signal.entry_zone_high,
        stop_loss=signal.stop_loss, take_profit=signal.take_profit_1, notified_at=at,
        features=Features(
            atr=context.atr,
            volume_ratio_last=context.volume_ratio,
            stop_pct=stop_percent(
                signal.direction, signal.entry_zone_low, signal.entry_zone_high,
                signal.stop_loss,
            ),
            breakout_volume_ratio=signal.breakout_volume_ratio,
            breakout_at=signal.breakout_at,
            ema50_distance_atr=signal.ema50_distance_atr,
        ),
        features_source="replay",
    )


def replay_series(
    symbol: str,
    tf: str,
    candles: Sequence[Kline],
    start: datetime,
    end: datetime,
    *,
    ttl: timedelta = TTL,
    evaluate: Callable[[MarketContext], Signal] = engine_evaluate,
    first_id: int = 1,
) -> list[Row]:
    """Скан на закрытии каждой свечи из [start; end] по CANDLES_REQUIRED
    закрытым свечам. Свеча closes at close_time — это и время уведомления."""
    return replay_multi(
        symbol, tf, candles, start, end, {"_": evaluate}, ttl=ttl, first_id=first_id
    )["_"]


def replay_multi(
    symbol: str,
    tf: str,
    candles: Sequence[Kline],
    start: datetime,
    end: datetime,
    evaluators: dict[str, Callable[[MarketContext], Signal]],
    *,
    ttl: timedelta = TTL,
    d1_ema: Callable[[datetime], Decimal | None] | None = None,
    first_id: int = 1,
) -> dict[str, list[Row]]:
    """Как replay_series, но контекст свечи строится один раз и прогоняется
    через все детекторы; у каждого — свой слот сканера. d1_ema — EMA200 D1
    на момент закрытия свечи (только закрытые дни) для FalseBreakout."""
    states = {name: SlotState() for name in evaluators}
    rows: dict[str, list[Row]] = {name: [] for name in evaluators}
    next_id = first_id
    for i, candle in enumerate(candles):
        at = candle.close_time
        if at < start or at > end:
            continue
        window = list(candles[max(0, i + 1 - CANDLES_REQUIRED): i + 1])
        if len(window) < 50:
            continue
        context = context_from_candles(
            symbol, tf, window, d1_ema200=d1_ema(at) if d1_ema is not None else None
        )
        for name, evaluate in evaluators.items():
            signal = evaluate(context)
            level = classify_signal(signal)
            fingerprint = (
                build_fingerprint(signal, level) if level is SignalLevel.READY else None
            )
            if slot_step(states[name], level, fingerprint, at, ttl):
                rows[name].append(row_from_signal(next_id, signal, context, symbol, tf, at))
                next_id += 1
    return rows


def d1_ema_lookup(d1: Sequence[Kline]) -> Callable[[datetime], Decimal | None]:
    """EMA200 по дневным закрытиям: на момент `at` — значение последнего дня,
    закрытого к `at`. EMA дня зависит только от дней до него — будущего нет."""
    times = [c.close_time for c in d1]
    values = ema([c.close for c in d1], D1_EMA_PERIOD)

    def at(moment: datetime) -> Decimal | None:
        i = bisect_right(times, moment) - 1
        return values[i] if i >= 0 else None

    return at


# --- исход, funding, режим ------------------------------------------------------


def funding_r(
    row: Row, start: datetime, end: datetime, events: Sequence[FundingEvent]
) -> Decimal | None:
    """Funding за удержание (start; end] в долях риска. LONG платит при
    положительной ставке, SHORT получает. Нет истории на весь срок — None."""
    if not events or events[0].time > start:
        return None
    paid = sum(
        (e.rate * e.mark_price for e in events if start < e.time <= end), Decimal(0)
    )
    sign = Decimal(1) if row.direction is SignalDirection.LONG else Decimal(-1)
    return -sign * paid / row.risk


def d1_regime(d1: Sequence[Kline], at: datetime, price: Decimal) -> str:
    """Цена против EMA200 по закрытым к моменту `at` дневным свечам."""
    closed = [c.close for c in d1 if c.close_time <= at]
    if len(closed) < D1_EMA_PERIOD:
        return "н/д"
    value = last_value(ema(closed, D1_EMA_PERIOD))
    if value is None:
        return "н/д"
    return "выше EMA200 D1" if price > value else "ниже EMA200 D1"


@dataclass(slots=True)
class Scored:
    result: Result
    r_funding: Decimal | None
    r_net_funding: Decimal | None
    regime: str
    exit_at: datetime | None = None


def score_row(
    row: Row,
    candles: Sequence[Kline],
    funding: Sequence[FundingEvent],
    d1: Sequence[Kline],
    *,
    rate: Decimal = FEE_RATE,
    horizon: int = HORIZON,
    now: datetime,
) -> Scored:
    forward = forward_window(candles, row.notified_at, now)
    outcome = simulate(row, forward, horizon)
    result = score(row, outcome, rate)
    price_at = next(
        (c.close for c in reversed(candles) if c.close_time <= row.notified_at), row.entry
    )
    regime = d1_regime(d1, row.notified_at, price_at)
    if outcome.bar is None or result.r_net is None:
        return Scored(result, None, None, regime)
    exit_at = row.notified_at + step(row.timeframe) * outcome.bar
    fr = funding_r(row, row.notified_at, exit_at, funding)
    return Scored(result, fr, None if fr is None else result.r_net + fr, regime, exit_at)


# --- сводки -------------------------------------------------------------------


@dataclass(slots=True)
class Line:
    label: str
    n: int
    takes: int
    stops: int
    open: int
    r_gross: Decimal | None
    r_net: Decimal | None
    r_net_funding: Decimal | None
    funding_missing: int


def _avg(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def line(label: str, items: Sequence[Scored]) -> Line:
    closed = [s for s in items if s.result.r_gross is not None]
    return Line(
        label=label,
        n=len(items),
        takes=sum(1 for s in items if s.result.outcome.kind == TAKE),
        stops=sum(1 for s in items if s.result.outcome.kind == STOP),
        open=sum(1 for s in items if s.result.outcome.kind == OPEN),
        r_gross=_avg([s.result.r_gross for s in closed if s.result.r_gross is not None]),
        r_net=_avg([s.result.r_net for s in closed if s.result.r_net is not None]),
        r_net_funding=_avg([s.r_net_funding for s in closed if s.r_net_funding is not None]),
        funding_missing=sum(1 for s in closed if s.r_net_funding is None),
    )


def first_per_breakout(items: Sequence[Scored]) -> tuple[list[Scored], list[Scored]]:
    """(первые на пробой, повторные) среди «пробоя с ретестом» с breakout_at."""
    seen: set[tuple[str, str, str, datetime]] = set()
    first: list[Scored] = []
    repeat: list[Scored] = []
    for s in sorted(items, key=lambda s: (s.result.row.notified_at, s.result.row.id)):
        row = s.result.row
        if row.setup != BreakoutRetest.name or row.features.breakout_at is None:
            continue
        key = (row.symbol, row.timeframe, row.direction.value, row.features.breakout_at)
        (repeat if key in seen else first).append(s)
        seen.add(key)
    return first, repeat


def stop_atr(row: Row) -> Decimal | None:
    atr = row.features.atr
    return None if not atr else row.risk / atr


def grouped(items: Sequence[Scored], key: Callable[[Scored], str]) -> list[Line]:
    groups: dict[str, list[Scored]] = {}
    for s in items:
        groups.setdefault(key(s), []).append(s)
    return [line(k, v) for k, v in sorted(groups.items())]


def sections(items: Sequence[Scored]) -> dict[str, list[Line]]:
    breakout = [s for s in items if s.result.row.setup == BreakoutRetest.name]
    first, repeat = first_per_breakout(items)
    return {
        "База": [line("все", items)],
        "Сетап": grouped(items, lambda s: s.result.row.setup),
        "Стоп, % от входа": grouped(
            items, lambda s: bucket(s.result.row.features.stop_pct, (Decimal(1),), "%")
        ),
        "Стоп в ATR": grouped(
            items, lambda s: bucket(stop_atr(s.result.row), (Decimal(1), Decimal(2)), " ATR")
        ),
        "Объём пробоя (пробой)": grouped(
            breakout,
            lambda s: bucket(s.result.row.features.breakout_volume_ratio, (Decimal("1.3"),)),
        ),
        "Один на пробой": [line("первый", first), line("повторные", repeat)],
        "Направление": grouped(items, lambda s: s.result.row.direction.value),
        "Режим D1 EMA200": grouped(items, lambda s: s.regime),
        "Символ": grouped(items, lambda s: s.result.row.symbol),
    }


# --- критерий половин ------------------------------------------------------------
#
# Кандидаты фиксированы до прогона (решение владельца 01.10): фильтр
# «оставить только …» подбирается на одной половине и принимается, только
# если держится на другой. R нетто без funding — у funding есть «н/д».

MIN_KEPT = 50
MIN_KEPT_SHARE = Decimal("0.3")
MIN_UPLIFT = Decimal("0.15")


def _trend_aligned(s: Scored) -> bool:
    d = s.result.row.direction
    return (d is SignalDirection.LONG and s.regime == "выше EMA200 D1") or (
        d is SignalDirection.SHORT and s.regime == "ниже EMA200 D1"
    )


def filter_candidates(
    symbols: Sequence[str], first_ids: set[int]
) -> dict[str, Callable[[Scored], bool]]:
    """first_ids — id() записей, первых на свой пробой (first_per_breakout)."""
    row = lambda s: s.result.row  # noqa: E731

    def first_only(s: Scored) -> bool:
        return id(s) in first_ids

    cands: dict[str, Callable[[Scored], bool]] = {
        "сетап: только пробой": lambda s: row(s).setup == BreakoutRetest.name,
        "сетап: только откат": lambda s: row(s).setup != BreakoutRetest.name,
        "стоп ≥1%": lambda s: (row(s).features.stop_pct or Decimal(0)) >= 1,
        "стоп <1%": lambda s: row(s).features.stop_pct is not None
        and row(s).features.stop_pct < 1,
        "стоп <1 ATR": lambda s: (v := stop_atr(row(s))) is not None and v < 1,
        "стоп 1–2 ATR": lambda s: (v := stop_atr(row(s))) is not None and 1 <= v < 2,
        "стоп ≥2 ATR": lambda s: (v := stop_atr(row(s))) is not None and v >= 2,
        "стоп ≥1 ATR": lambda s: (v := stop_atr(row(s))) is not None and v >= 1,
        "объём пробоя ≥1.3 (откаты остаются)": lambda s: row(s).setup != BreakoutRetest.name
        or (row(s).features.breakout_volume_ratio or Decimal(0)) >= Decimal("1.3"),
        "объём пробоя <1.3 (откаты остаются)": lambda s: row(s).setup != BreakoutRetest.name
        or (
            row(s).features.breakout_volume_ratio is not None
            and row(s).features.breakout_volume_ratio < Decimal("1.3")
        ),
        "один на пробой (откаты остаются)": lambda s: row(s).setup != BreakoutRetest.name
        or first_only(s),
        "только LONG": lambda s: row(s).direction is SignalDirection.LONG,
        "только SHORT": lambda s: row(s).direction is SignalDirection.SHORT,
        "режим: выше EMA200 D1": lambda s: s.regime == "выше EMA200 D1",
        "режим: ниже EMA200 D1": lambda s: s.regime == "ниже EMA200 D1",
        "по тренду D1 (LONG выше, SHORT ниже)": _trend_aligned,
    }
    for symbol in symbols:
        cands[f"без {symbol}"] = lambda s, sym=symbol: row(s).symbol != sym
    return cands


@dataclass(slots=True)
class FilterCheck:
    name: str
    fit_n: int
    fit_base: Decimal | None
    fit_r: Decimal | None
    test_n: int
    test_base: Decimal | None
    test_r: Decimal | None
    eligible: bool
    chosen: bool = False

    @property
    def uplift(self) -> Decimal | None:
        if self.test_r is None or self.test_base is None:
            return None
        return self.test_r - self.test_base

    @property
    def holds(self) -> bool:
        return (
            self.uplift is not None and self.uplift >= MIN_UPLIFT
            and self.test_r is not None and self.test_r > 0
        )


def _closed_net(items: Sequence[Scored]) -> tuple[int, Decimal | None]:
    nets = [s.result.r_net for s in items if s.result.r_net is not None]
    return len(nets), _avg(nets)


def evaluate_filters(
    fit: Sequence[Scored], test: Sequence[Scored], symbols: Sequence[str]
) -> list[FilterCheck]:
    """Каждый кандидат: n и R нетто закрытых на половине подбора и на
    проверочной, против базы. Допустим к выбору — оставил ≥ MIN_KEPT и
    ≥ MIN_KEPT_SHARE закрытых на половине подбора. Выбран — лучший R нетто
    среди допустимых. Держится — на проверочной R нетто выше базы на
    MIN_UPLIFT и выше нуля."""
    first_ids: set[int] = set()
    for half in (fit, test):
        first, _ = first_per_breakout(half)
        first_ids.update(id(s) for s in first)
    cands = filter_candidates(symbols, first_ids)
    fit_total, fit_base = _closed_net(fit)
    _, test_base = _closed_net(test)
    checks: list[FilterCheck] = []
    for name, keep in cands.items():
        fit_n, fit_r = _closed_net([s for s in fit if keep(s)])
        test_n, test_r = _closed_net([s for s in test if keep(s)])
        eligible = fit_n >= MIN_KEPT and fit_total > 0 and (
            Decimal(fit_n) / Decimal(fit_total) >= MIN_KEPT_SHARE
        )
        checks.append(FilterCheck(
            name, fit_n, fit_base, fit_r, test_n, test_base, test_r, eligible
        ))
    best = max(
        (c for c in checks if c.eligible and c.fit_r is not None),
        key=lambda c: c.fit_r or Decimal(0), default=None,
    )
    if best is not None:
        best.chosen = True
    return checks


def render_filters(title: str, checks: Sequence[FilterCheck]) -> str:
    out = [
        f"\n**{title}**\n",
        "| фильтр | n подбор | R нетто подбор (база) | n проверка | R нетто проверка (база) "
        "| прирост | допуск | выбран | держится |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in sorted(checks, key=lambda c: (not c.chosen, -(c.fit_r or Decimal(-99)))):
        out.append(
            f"| {c.name} | {c.fit_n} | {_fmt(c.fit_r)} ({_fmt(c.fit_base)}) | {c.test_n} "
            f"| {_fmt(c.test_r)} ({_fmt(c.test_base)}) | {_fmt(c.uplift)} "
            f"| {'да' if c.eligible else 'нет'} | {'★' if c.chosen else ''} "
            f"| {'да' if c.holds else 'нет'} |"
        )
    return "\n".join(out)


def notification_counts(
    items: Sequence[Scored], halves: tuple[datetime, datetime, datetime]
) -> str:
    """Уведомления по символу и половине, в сутки — правдоподобность против
    живого темпа."""
    start, middle, end = halves
    days = [(middle - start).total_seconds() / 86400, (end - middle).total_seconds() / 86400]
    symbols = sorted({s.result.row.symbol for s in items})
    out = ["| символ | половина 1 | в сутки | половина 2 | в сутки |", "|---|---|---|---|---|"]
    for sym in [*symbols, "все"]:
        sel = [s for s in items if sym == "все" or s.result.row.symbol == sym]
        a = sum(1 for s in sel if s.result.row.notified_at < middle)
        b = len(sel) - a
        out.append(f"| {sym} | {a} | {a / days[0]:.2f} | {b} | {b / days[1]:.2f} |")
    return "\n".join(out)


def funding_coverage(items: Sequence[Scored]) -> str:
    closed = [s for s in items if s.result.r_net is not None]
    missing = sum(1 for s in closed if s.r_net_funding is None)
    share = Decimal(missing) / Decimal(len(closed)) * 100 if closed else Decimal(0)
    return f"закрытых {len(closed)}, без funding {missing} ({share:.0f}%)"


def split_halves(
    items: Sequence[Scored], start: datetime, end: datetime
) -> tuple[list[Scored], list[Scored]]:
    middle = start + (end - start) / 2
    first = [s for s in items if s.result.row.notified_at < middle]
    second = [s for s in items if s.result.row.notified_at >= middle]
    return first, second


def _fmt(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def render_lines(title: str, lines: Sequence[Line]) -> str:
    out = [
        f"\n**{title}**\n",
        "| срез | n | тейк/стоп/откр | R брутто | R нетто | R нетто+funding | funding н/д |",
        "|---|---|---|---|---|---|---|",
    ]
    for ln in lines:
        out.append(
            f"| {ln.label} | {ln.n} | {ln.takes}/{ln.stops}/{ln.open} | {_fmt(ln.r_gross)} "
            f"| {_fmt(ln.r_net)} | {_fmt(ln.r_net_funding)} | {ln.funding_missing} |"
        )
    return "\n".join(out)


# --- ложный пробой по тренду (--strategy fbo) ------------------------------------
#
# Список конфигураций зафиксирован владельцем до прогона (01.10) и после
# него не расширяется: 4 базы d × N, поверх каждой по одному фильтру.

FBO_BASES = ((Decimal("0.1"), 1), (Decimal("0.1"), 3), (Decimal("0.3"), 1), (Decimal("0.3"), 3))
FBO_VOLUME = Decimal("1.3")
OLD_STRATEGY = "старая: пробой + откат (сканер)"
MIN_CLOSED_FBO = 30
CI_LEVEL = 0.9
CI_FLOOR = Decimal("-0.1")
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261001


@dataclass(frozen=True, slots=True)
class FboConfig:
    name: str
    detector: str     # ключ evaluators в replay_multi
    funding: bool     # фильтр funding поверх строк детектора


def fbo_setup() -> tuple[dict[str, Callable[[MarketContext], Signal]], list[FboConfig]]:
    """Детекторы для replay_multi и 16 конфигураций (+ старая стратегия для
    сравнения). Конфигурация с funding — те же строки базы, отфильтрованные:
    отдельный детектор ей не нужен."""
    evaluators: dict[str, Callable[[MarketContext], Signal]] = {OLD_STRATEGY: engine_evaluate}
    configs = [FboConfig(OLD_STRATEGY, OLD_STRATEGY, False)]
    for d, n in FBO_BASES:
        base = f"d={d} N={n}"
        evaluators[base] = FalseBreakout(probe_atr=d, return_bars=n).detect
        evaluators[f"{base} объём"] = FalseBreakout(
            probe_atr=d, return_bars=n, min_probe_volume_ratio=FBO_VOLUME
        ).detect
        evaluators[f"{base} подтв"] = FalseBreakout(
            probe_atr=d, return_bars=n, confirm_next=True
        ).detect
        configs += [
            FboConfig(f"{base} · база", base, False),
            FboConfig(f"{base} · объём ≥{FBO_VOLUME}", f"{base} объём", False),
            FboConfig(f"{base} · подтверждение", f"{base} подтв", False),
            FboConfig(f"{base} · funding", base, True),
        ]
    return evaluators, configs


def funding_allows(row: Row, events: Sequence[FundingEvent]) -> bool:
    """SHORT — только при последней ставке > 0, LONG — только < 0. Ставка —
    последнее начисление не позже уведомления; нет такого или ноль — нет."""
    times = [e.time for e in events]
    i = bisect_right(times, row.notified_at) - 1
    if i < 0:
        return False
    rate = events[i].rate
    return rate > 0 if row.direction is SignalDirection.SHORT else rate < 0


def bootstrap_ci(
    values: Sequence[Decimal], *, level: float = CI_LEVEL,
    resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED,
) -> tuple[Decimal, Decimal] | None:
    """Перцентильный бутстрэп-интервал среднего; сид фиксирован —
    повторный прогон даёт те же границы. Меньше 2 значений — None."""
    if len(values) < 2:
        return None
    xs = [float(v) for v in values]
    n = len(xs)
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(xs, k=n)) / n for _ in range(resamples))
    lo = means[int(resamples * (1 - level) / 2)]
    hi = means[int(resamples * (1 + level) / 2) - 1]
    return Decimal(f"{lo:.4f}"), Decimal(f"{hi:.4f}")


@dataclass(slots=True)
class HalfStats:
    n: int
    closed: int
    per_day: Decimal
    win_pct: Decimal | None
    r_gross: Decimal | None
    r_net: Decimal | None
    r_nf: Decimal | None
    ci: tuple[Decimal, Decimal] | None
    short: tuple[int, int, Decimal | None]   # n, тейков, R нетто+funding
    long: tuple[int, int, Decimal | None]


def _side(items: Sequence[Scored], direction: SignalDirection) -> tuple[int, int, Decimal | None]:
    sel = [s for s in items if s.result.row.direction is direction]
    takes = sum(1 for s in sel if s.result.outcome.kind == TAKE)
    return len(sel), takes, _avg([s.r_net_funding for s in sel if s.r_net_funding is not None])


def half_stats(items: Sequence[Scored], days: float) -> HalfStats:
    closed = [s for s in items if s.result.r_net is not None]
    nf = [s.r_net_funding for s in closed if s.r_net_funding is not None]
    takes = sum(1 for s in closed if s.result.outcome.kind == TAKE)
    return HalfStats(
        n=len(items),
        closed=len(closed),
        per_day=Decimal(f"{len(items) / days:.2f}") if days else Decimal(0),
        win_pct=Decimal(takes * 100) / Decimal(len(closed)) if closed else None,
        r_gross=_avg([s.result.r_gross for s in closed if s.result.r_gross is not None]),
        r_net=_avg([s.result.r_net for s in closed if s.result.r_net is not None]),
        r_nf=_avg(nf),
        ci=bootstrap_ci(nf),
        short=_side(items, SignalDirection.SHORT),
        long=_side(items, SignalDirection.LONG),
    )


def half_ok(h: HalfStats) -> bool:
    """Половина «держится»: ≥ 30 закрытых, R нетто+funding > 0, нижняя
    граница 90% интервала > −0.1R."""
    return (
        h.closed >= MIN_CLOSED_FBO and h.r_nf is not None and h.r_nf > 0
        and h.ci is not None and h.ci[0] > CI_FLOOR
    )


def fbo_select(
    stats: dict[str, tuple[HalfStats, HalfStats]], fit: int
) -> tuple[str, HalfStats, HalfStats] | None:
    """Лучшая по R нетто+funding на половине `fit` (0/1) среди FBO-конфигураций
    с ≥ 30 закрытых; возвращает её и обе половины. Старая стратегия — не кандидат."""
    cands = [
        (name, h) for name, h in stats.items()
        if name != OLD_STRATEGY and h[fit].closed >= MIN_CLOSED_FBO and h[fit].r_nf is not None
    ]
    if not cands:
        return None
    name, h = max(cands, key=lambda c: c[1][fit].r_nf or Decimal(-99))
    return name, h[fit], h[1 - fit]


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def _ci(ci: tuple[Decimal, Decimal] | None) -> str:
    return "—" if ci is None else f"[{ci[0]:+.2f}; {ci[1]:+.2f}]"


def _side_fmt(side: tuple[int, int, Decimal | None]) -> str:
    n, takes, r = side
    return f"{n} · {takes} · {_fmt(r)}" if n else "0"


def render_fbo(
    tf: str,
    stats: dict[str, tuple[HalfStats, HalfStats]],
    bounds: tuple[datetime, datetime, datetime],
) -> str:
    start, middle, end = bounds
    n_fbo = sum(1 for name in stats if name != OLD_STRATEGY)
    out = [f"\n# {tf}: ложный пробой по тренду — конфигураций {n_fbo} и старая стратегия"]
    for half, title in ((0, f"первая половина {start:%d.%m.%Y}–{middle:%d.%m.%Y}"),
                        (1, f"вторая половина {middle:%d.%m.%Y}–{end:%d.%m.%Y}")):
        out += [
            f"\n## {tf} — {title}\n",
            "| конфигурация | n | закрытых | в сутки | % тейков | R брутто | R нетто "
            "| R нетто+funding | ДИ90 R нетто+f | SHORT n·тейк·R | LONG n·тейк·R |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for name, h in stats.items():
            s = h[half]
            out.append(
                f"| {name} | {s.n} | {s.closed} | {s.per_day} | {_pct(s.win_pct)} "
                f"| {_fmt(s.r_gross)} | {_fmt(s.r_net)} | {_fmt(s.r_nf)} | {_ci(s.ci)} "
                f"| {_side_fmt(s.short)} | {_side_fmt(s.long)} |"
            )
    directions = ((0, "подбор на 1-й → проверка на 2-й"), (1, "подбор на 2-й → проверка на 1-й"))
    if n_fbo > 1:
        out.append(
            f"\n## {tf} — отбор в обе стороны (по R нетто+funding, ≥{MIN_CLOSED_FBO} закрытых)\n"
        )
    for fit, label in directions if n_fbo > 1 else ():
        sel = fbo_select(stats, fit)
        if sel is None:
            out.append(f"- {label}: нет конфигураций с ≥{MIN_CLOSED_FBO} закрытых")
            continue
        name, fit_h, test_h = sel
        out.append(
            f"- {label}: **{name}** — подбор {_fmt(fit_h.r_nf)} {_ci(fit_h.ci)} "
            f"(n {fit_h.closed}); "
            f"проверка {_fmt(test_h.r_nf)} {_ci(test_h.ci)} (n {test_h.closed}) — "
            f"{'держится' if half_ok(test_h) else 'не держится'}"
        )
    out += [
        f"\n## {tf} — принятие: обе половины ≥{MIN_CLOSED_FBO} закрытых, R нетто+funding > 0, "
        f"нижняя граница ДИ90 > {CI_FLOOR}\n",
        "| конфигурация | 1-я: закрытых · R · ДИ90 | 2-я: закрытых · R · ДИ90 | принята |",
        "|---|---|---|---|",
    ]
    for name, (a, b) in stats.items():
        verdict = "да" if half_ok(a) and half_ok(b) else "нет"
        if name == OLD_STRATEGY:
            verdict += " (для сравнения)"
        out.append(
            f"| {name} | {a.closed} · {_fmt(a.r_nf)} · {_ci(a.ci)} "
            f"| {b.closed} · {_fmt(b.r_nf)} · {_ci(b.ci)} | {verdict} |"
        )
    return "\n".join(out)


def regime_stats(items: Sequence[Scored], days: float) -> dict[str, HalfStats]:
    return {
        regime: half_stats([s for s in items if s.regime == regime], days)
        for regime in sorted({s.regime for s in items})
    }


def render_regimes(
    tf: str, regimes: dict[str, tuple[dict[str, HalfStats], dict[str, HalfStats]]]
) -> str:
    """Режим D1 EMA200 (цена против EMA200 по закрытым дням на момент
    сигнала) по половинам: закрытых · R нетто+funding · ДИ90."""
    out = [
        f"\n## {tf} — режим D1 EMA200 по половинам\n",
        "| конфигурация | режим | 1-я: n · закрытых · R · ДИ90 | 2-я: n · закрытых · R · ДИ90 |",
        "|---|---|---|---|",
    ]
    for name, (a, b) in regimes.items():
        for regime in sorted(set(a) | set(b)):
            cells = []
            for half in (a, b):
                h = half.get(regime)
                cells.append(
                    "0" if h is None
                    else f"{h.n} · {h.closed} · {_fmt(h.r_nf)} · {_ci(h.ci)}"
                )
            out.append(f"| {name} | {regime} | {cells[0]} | {cells[1]} |")
    return "\n".join(out)


def select_fbo(
    names: Sequence[str] | None,
) -> tuple[dict[str, Callable[[MarketContext], Signal]], list[FboConfig]]:
    """Только названные конфигурации (точные имена из fbo_setup) плюс старая
    стратегия для справки; детекторы — только нужные им. None — все."""
    evaluators, configs = fbo_setup()
    if names is None:
        return evaluators, configs
    known = {c.name: c for c in configs}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"неизвестные конфигурации FBO: {unknown}; есть: {list(known)}")
    chosen = [known[OLD_STRATEGY], *(known[n] for n in names if n != OLD_STRATEGY)]
    return {c.detector: evaluators[c.detector] for c in chosen}, chosen


def funding_gap(symbol: str, funding: Sequence[FundingEvent], start: datetime) -> str | None:
    """Funding должен покрывать начало окна: фильтр и R funding берут ставку
    последнего начисления до сигнала. Иначе символ исключается."""
    if not funding:
        return f"{symbol} (funding нет)"
    if funding[0].time > start:
        return f"{symbol} (funding с {funding[0].time:%d.%m.%Y})"
    return None


# --- калибровка ---------------------------------------------------------------


def load_live(path: Path) -> list[Row]:
    """JSON signal_outcomes --json: строки живых READY."""
    rows: list[Row] = []
    for d in json.loads(path.read_text(encoding="utf-8")):
        rows.append(Row(
            id=int(d["id"]), symbol=d["symbol"], timeframe=d["timeframe"], setup=d["setup"],
            direction=SignalDirection(d["direction"]),
            entry_low=Decimal(str(d["entry_low"])), entry_high=Decimal(str(d["entry_high"])),
            stop_loss=Decimal(str(d["stop_loss"])), take_profit=Decimal(str(d["take_profit"])),
            notified_at=datetime.fromisoformat(d["notified_at"]),
        ))
    return rows


def match_live(live: Sequence[Row], replayed: Sequence[Row]) -> list[tuple[Row, Row | None]]:
    """Живое уведомление → replay с тем же символом, ТФ, направлением, стопом
    и тейком в окне [−MATCH_BARS_BEFORE; +MATCH_BARS_AFTER] свечей."""
    out: list[tuple[Row, Row | None]] = []
    for lv in live:
        lo = lv.notified_at - step(lv.timeframe) * MATCH_BARS_BEFORE
        hi = lv.notified_at + step(lv.timeframe) * MATCH_BARS_AFTER
        found = next(
            (
                r for r in replayed
                if r.symbol == lv.symbol and r.timeframe == lv.timeframe
                and r.direction is lv.direction
                and r.stop_loss == lv.stop_loss and r.take_profit == lv.take_profit
                and lo <= r.notified_at <= hi
            ),
            None,
        )
        out.append((lv, found))
    return out


def calibration_passed(matched: int, total: int) -> bool:
    return total > 0 and Decimal(matched) / Decimal(total) >= CALIBRATION_THRESHOLD


# --- оркестрация ----------------------------------------------------------------


async def cached_klines(
    fetcher: KlineFetcher, cache: Path, symbol: str, tf: str, start: datetime, end: datetime
) -> list[Kline]:
    """Из кэша, если он покрывает [start; end − 2 свечи]; иначе с биржи заново."""
    path = cache / f"{symbol}_{tf}.jsonl.gz"
    if path.exists():
        cached = load_klines(path, tf)
        if cached and cached[0].open_time <= start and cached[-1].close_time >= end - step(tf) * 2:
            return [c for c in cached if c.open_time >= start and c.close_time <= end]
    candles = await fetch_klines(fetcher, symbol, tf, start, end)
    save_klines(path, candles)
    return candles


async def cached_funding(
    fetcher: FundingFetcher, cache: Path, symbol: str, start: datetime, end: datetime
) -> list[FundingEvent]:
    path = cache / f"{symbol}_funding.jsonl.gz"
    if path.exists():
        cached = load_funding(path)
        if cached and cached[-1].time >= end - timedelta(hours=12):
            return cached
    events = await fetch_funding(fetcher, symbol, start, end)
    save_funding(path, events)
    return events


def _public_client() -> Any:
    from app.exchanges.bingx import BingXClient

    return BingXClient()


async def calibrate(live_path: Path, cache: Path) -> int:
    live = [r for r in load_live(live_path) if r.timeframe in REPLAY_TIMEFRAMES]
    start = min(r.notified_at for r in live) - timedelta(days=1)
    now = datetime.now(UTC)
    symbols = sorted({r.symbol for r in live})
    client = _public_client()
    fetcher = KlineFetcher(client)
    replayed: list[Row] = []
    try:
        for symbol in symbols:
            for tf in REPLAY_TIMEFRAMES:
                warm = start - step(tf) * (CANDLES_REQUIRED + 5)
                candles = await cached_klines(fetcher, cache, symbol, tf, warm, now)
                replayed += replay_series(
                    symbol, tf, candles, start, now, first_id=len(replayed) + 1
                )
    except RateLimitStopError as exc:
        print(f"СТОП по лимиту BingX: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.close()

    pairs = match_live(live, replayed)
    matched = sum(1 for _, r in pairs if r is not None)
    print(f"Калибровка: живых READY {len(live)} ({live[0].notified_at:%d.%m}–"
          f"{max(r.notified_at for r in live):%d.%m %H:%M} UTC), replay READY "
          f"{len(replayed)} за то же окно, запросов klines {fetcher.requests}")
    for tf in REPLAY_TIMEFRAMES:
        sub = [(lv, r) for lv, r in pairs if lv.timeframe == tf]
        n_ok = sum(1 for _, r in sub if r is not None)
        n_rep = sum(1 for r in replayed if r.timeframe == tf)
        print(f"  {tf}: найдено {n_ok}/{len(sub)}, replay уведомлений {n_rep}")
    pct = Decimal(matched) / Decimal(len(pairs)) * 100 if pairs else Decimal(0)
    print(f"Совпадение: {matched}/{len(pairs)} = {pct:.0f}% (порог 80%)")
    for lv, r in pairs:
        if r is None:
            near = [
                x for x in replayed
                if x.symbol == lv.symbol and x.timeframe == lv.timeframe
                and abs(x.notified_at - lv.notified_at) <= step(lv.timeframe) * 6
            ]
            hint = "; ".join(
                f"{x.notified_at:%d.%m %H:%M} {x.direction.value} стоп {x.stop_loss.normalize()} "
                f"тейк {x.take_profit.normalize()}"
                for x in near[:2]
            ) or "рядом нет"
            print(f"  не найдено #{lv.id} {lv.symbol} {lv.timeframe} {lv.direction.value} "
                  f"{lv.notified_at:%d.%m %H:%M} стоп {lv.stop_loss.normalize()} тейк "
                  f"{lv.take_profit.normalize()} | replay рядом: {hint}")
    ok = calibration_passed(matched, len(pairs))
    print("ИТОГ КАЛИБРОВКИ:", "OK" if ok else "НИЖЕ ПОРОГА — основной прогон не запускать")
    return 0 if ok else 3


def history_gap(symbol: str, data: dict[str, list[Kline]], start: datetime) -> str | None:
    """Текст причины исключения, если у какого-то ТФ нет истории с прогревом
    до start; None — всё есть (или ТФ ещё не загружен)."""
    for tf, candles in data.items():
        warm = start - step(tf) * CANDLES_REQUIRED
        if not candles:
            return f"{symbol} ({tf}: нет свечей)"
        if candles[0].open_time > warm:
            return f"{symbol} ({tf}: история с {candles[0].open_time:%d.%m.%Y})"
    return None


async def run(
    months: int,
    symbols: Sequence[str],
    cache: Path,
    json_path: Path | None,
    *,
    strategy: str = "breakout",
    timeframes: Sequence[str] = REPLAY_TIMEFRAMES,
    end: datetime | None = None,
    fbo_configs: Sequence[str] | None = None,
) -> int:
    """end — конец окна (по умолчанию сейчас): свечи и исходы — только
    закрытые к нему; так прогоны разных стратегий сравнимы на одних данных."""
    now = end or datetime.now(UTC)
    start = now - timedelta(days=round(months * 365 / 12))
    if strategy == "fbo":
        return await run_fbo(start, now, symbols, cache, json_path, timeframes, fbo_configs)
    client = _public_client()
    fetcher = KlineFetcher(client)
    ffetcher = FundingFetcher(client)
    scored: list[Scored] = []
    excluded: list[str] = []
    funding_from: dict[str, datetime | None] = {}
    try:
        for symbol in symbols:
            data: dict[str, list[Kline]] = {}
            gap: str | None = None
            for tf in timeframes:
                warm = start - step(tf) * (CANDLES_REQUIRED + 5)
                data[tf] = await cached_klines(fetcher, cache, symbol, tf, warm, now)
                gap = history_gap(symbol, data, start)
                if gap is not None:
                    break
            if gap is not None:
                excluded.append(gap)
                continue
            d1 = await cached_klines(
                fetcher, cache, symbol, "1d", start - timedelta(days=D1_EMA_PERIOD + 30), now
            )
            funding = await cached_funding(ffetcher, cache, symbol, start, now)
            funding_from[symbol] = funding[0].time if funding else None
            for tf in timeframes:
                rows = replay_series(symbol, tf, data[tf], start, now, first_id=len(scored) + 1)
                scored += [score_row(r, data[tf], funding, d1, now=now) for r in rows]
            print(f"{symbol}: готово, всего READY {len(scored)}", file=sys.stderr)
    except RateLimitStopError as exc:
        print(f"СТОП по лимиту BingX: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.close()

    print(f"Replay {start:%d.%m.%Y}–{now:%d.%m.%Y %H:%M} UTC, горизонт {HORIZON}, "
          f"taker {FEE_RATE}, TTL слота {TTL}; запросов klines {fetcher.requests}, "
          f"funding {ffetcher.requests} (повторов на data: null — {ffetcher.null_retries})")
    if excluded:
        print("Исключены (нет полной истории): " + ", ".join(excluded))
    print("Funding с: " + ", ".join(
        f"{s} {t:%d.%m.%Y}" if t else f"{s} нет" for s, t in funding_from.items()
    ))
    for tf in timeframes:
        items = [s for s in scored if s.result.row.timeframe == tf]
        halves = split_halves(items, start, now)
        middle = start + (now - start) / 2
        print(f"\n# {tf}: уведомления по символу и половине "
              f"(половины {start:%d.%m.%Y}–{middle:%d.%m.%Y}–{now:%d.%m.%Y})\n")
        print(notification_counts(items, (start, middle, now)))
        for name, half in zip(("первая половина", "вторая половина"), halves, strict=True):
            print(f"\n## {tf} — {name} (n={len(half)}); funding: {funding_coverage(half)}")
            for title, lines in sections(half).items():
                print(render_lines(title, lines))
        used = [s for s in symbols if s not in {e.split(" ")[0] for e in excluded}]
        print(render_filters(
            f"{tf}: фильтры — подбор на первой половине, проверка на второй",
            evaluate_filters(halves[0], halves[1], used),
        ))
        print(render_filters(
            f"{tf}: фильтры — подбор на второй половине, проверка на первой (устойчивость)",
            evaluate_filters(halves[1], halves[0], used),
        ))
    if json_path is not None:
        json_path.write_text(json.dumps([
            {
                **{k: v for k, v in asdict(s.result.row).items() if k != "features"},
                **asdict(s.result.row.features),
                "outcome": s.result.outcome.kind, "bar": s.result.outcome.bar,
                "r_gross": s.result.r_gross, "r_net": s.result.r_net,
                "r_funding": s.r_funding, "r_net_funding": s.r_net_funding,
                "regime": s.regime,
            }
            for s in scored
        ], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


async def load_symbol(
    fetcher: KlineFetcher, ffetcher: FundingFetcher, cache: Path, symbol: str,
    timeframes: Sequence[str], start: datetime, now: datetime,
) -> tuple[dict[str, list[Kline]], list[Kline], list[FundingEvent]] | str:
    """Свечи ТФ, дневные и funding символа; строка — причина исключения."""
    data: dict[str, list[Kline]] = {}
    for tf in timeframes:
        warm = start - step(tf) * (CANDLES_REQUIRED + 5)
        data[tf] = await cached_klines(fetcher, cache, symbol, tf, warm, now)
        gap = history_gap(symbol, data, start)
        if gap is not None:
            return gap
    d1 = await cached_klines(
        fetcher, cache, symbol, "1d", start - timedelta(days=D1_EMA_PERIOD + 30), now
    )
    funding = await cached_funding(ffetcher, cache, symbol, start, now)
    gap = funding_gap(symbol, funding, start)
    if gap is not None:
        return gap
    return data, d1, funding


async def coverage(
    start: datetime, now: datetime, symbols: Sequence[str], cache: Path,
    timeframes: Sequence[str],
) -> int:
    """До прогона: покрывают ли свечи (с прогревом), дневные и funding окно.
    Качает в кэш; печатает по символу первую свечу/начисление против нужного."""
    client = _public_client()
    fetcher = KlineFetcher(client)
    ffetcher = FundingFetcher(client)
    print(f"Окно {start:%d.%m.%Y %H:%M}–{now:%d.%m.%Y %H:%M} UTC; нужно: свечи ТФ с прогревом "
          f"{CANDLES_REQUIRED}, дневные с {start - timedelta(days=D1_EMA_PERIOD):%d.%m.%Y} "
          f"(EMA200), funding с {start:%d.%m.%Y}\n")
    print("| символ | " + " | ".join(f"{tf} с" for tf in timeframes)
          + " | 1d с | funding с | итог |")
    print("|---|" + "---|" * (len(timeframes) + 3))
    try:
        for symbol in symbols:
            cells = []
            for tf in timeframes:
                warm = start - step(tf) * (CANDLES_REQUIRED + 5)
                c = await cached_klines(fetcher, cache, symbol, tf, warm, now)
                cells.append(f"{c[0].open_time:%d.%m.%Y}" if c else "нет")
            d1 = await cached_klines(
                fetcher, cache, symbol, "1d", start - timedelta(days=D1_EMA_PERIOD + 30), now
            )
            funding = await cached_funding(ffetcher, cache, symbol, start, now)
            loaded = await load_symbol(fetcher, ffetcher, cache, symbol, timeframes, start, now)
            d1_ok = len([k for k in d1 if k.close_time <= start]) >= D1_EMA_PERIOD
            verdict = loaded if isinstance(loaded, str) else (
                "ок" if d1_ok else f"{symbol} (дневных до окна < {D1_EMA_PERIOD})"
            )
            print(f"| {symbol} | " + " | ".join(cells)
                  + " | " + (f"{d1[0].open_time:%d.%m.%Y}" if d1 else "нет") + " | "
                  + (f"{funding[0].time:%d.%m.%Y}" if funding else "нет") + f" | {verdict} |")
    except RateLimitStopError as exc:
        print(f"СТОП по лимиту BingX: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.close()
    print(f"\nзапросов klines {fetcher.requests}, funding {ffetcher.requests}")
    return 0


async def run_fbo(
    start: datetime, now: datetime, symbols: Sequence[str], cache: Path,
    json_path: Path | None, timeframes: Sequence[str],
    config_names: Sequence[str] | None = None,
) -> int:
    client = _public_client()
    fetcher = KlineFetcher(client)
    ffetcher = FundingFetcher(client)
    evaluators, configs = select_fbo(config_names)
    results: dict[str, dict[str, list[Scored]]] = {
        tf: {c.name: [] for c in configs} for tf in timeframes
    }
    excluded: list[str] = []
    next_id = 1
    try:
        for symbol in symbols:
            loaded = await load_symbol(fetcher, ffetcher, cache, symbol, timeframes, start, now)
            if isinstance(loaded, str):
                excluded.append(loaded)
                continue
            data, d1, funding = loaded
            lookup = d1_ema_lookup(d1)
            for tf in timeframes:
                rows = replay_multi(
                    symbol, tf, data[tf], start, now, evaluators, d1_ema=lookup,
                    first_id=next_id,
                )
                next_id += sum(len(v) for v in rows.values())
                scored = {
                    key: [score_row(r, data[tf], funding, d1, now=now) for r in rs]
                    for key, rs in rows.items()
                }
                for c in configs:
                    items = scored[c.detector]
                    if c.funding:
                        items = [s for s in items if funding_allows(s.result.row, funding)]
                    results[tf][c.name] += items
            print(f"{symbol}: готово", file=sys.stderr)
    except RateLimitStopError as exc:
        print(f"СТОП по лимиту BingX: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.close()

    middle = start + (now - start) / 2
    days = ((middle - start).total_seconds() / 86400, (now - middle).total_seconds() / 86400)
    print(f"Replay FBO {start:%d.%m.%Y %H:%M}–{now:%d.%m.%Y %H:%M} UTC, горизонт {HORIZON}, "
          f"taker {FEE_RATE}, TTL слота {TTL}; запросов klines {fetcher.requests}, "
          f"funding {ffetcher.requests}; бутстрэп {BOOTSTRAP_RESAMPLES}, сид {BOOTSTRAP_SEED}")
    if excluded:
        print("Исключены (нет полной истории): " + ", ".join(excluded))
    print(f"Конфигураций FBO: {len(configs) - 1}; множественное сравнение — "
          f"{len(configs) - 1} × {len(timeframes)} ТФ, критерий строгий намеренно")
    for tf in timeframes:
        stats: dict[str, tuple[HalfStats, HalfStats]] = {}
        for c in configs:
            h1, h2 = split_halves(results[tf][c.name], start, now)
            stats[c.name] = (half_stats(h1, days[0]), half_stats(h2, days[1]))
        print(render_fbo(tf, stats, (start, middle, now)))
        regimes: dict[str, tuple[dict[str, HalfStats], dict[str, HalfStats]]] = {}
        for c in configs:
            h1, h2 = split_halves(results[tf][c.name], start, now)
            regimes[c.name] = (regime_stats(h1, days[0]), regime_stats(h2, days[1]))
        print(render_regimes(tf, regimes))
    if json_path is not None:
        json_path.write_text(json.dumps([
            {
                "config": name,
                **{k: v for k, v in asdict(s.result.row).items() if k != "features"},
                "outcome": s.result.outcome.kind, "bar": s.result.outcome.bar,
                "r_gross": s.result.r_gross, "r_net": s.result.r_net,
                "r_funding": s.r_funding, "r_net_funding": s.r_net_funding, "regime": s.regime,
            }
            for tf in timeframes for name, items in results[tf].items() for s in items
        ], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


def parse_end(value: str) -> datetime:
    """ISO 8601 с зоной (2026-10-01T16:13Z); без зоны — ошибка, не догадка."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError(f"--end {value}: нужна зона (Z или +00:00)")
    return moment.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, default=CACHE_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    cal = sub.add_parser("calibrate", help="сверка replay с живыми READY")
    cal.add_argument("--live", type=Path, required=True, help="JSON signal_outcomes --json")
    rn = sub.add_parser("run", help="основной прогон")
    rn.add_argument("--months", type=int, default=12)
    rn.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    rn.add_argument("--json", dest="json_path", type=Path)
    rn.add_argument("--strategy", choices=("breakout", "fbo"), default="breakout")
    rn.add_argument(
        "--timeframes", help="через запятую; по умолчанию 1h,4h (breakout) или 4h,1h (fbo)"
    )
    rn.add_argument("--end", type=parse_end, help="конец окна, ISO с зоной; по умолчанию сейчас")
    rn.add_argument(
        "--fbo-config", action="append", dest="fbo_configs",
        help="только эта конфигурация FBO (точное имя; можно несколько); старая — всегда",
    )
    cov = sub.add_parser("coverage", help="до прогона: покрывают ли свечи и funding окно")
    cov.add_argument("--months", type=int, default=12)
    cov.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    cov.add_argument("--timeframes", default="4h")
    cov.add_argument("--end", type=parse_end, required=True)
    args = parser.parse_args(argv)
    if args.command == "calibrate":
        return asyncio.run(calibrate(args.live, args.cache))
    if args.command == "coverage":
        end = args.end
        return asyncio.run(coverage(
            end - timedelta(days=round(args.months * 365 / 12)), end,
            [s for s in args.symbols.split(",") if s], args.cache,
            [t for t in args.timeframes.split(",") if t],
        ))
    if args.fbo_configs:
        try:
            select_fbo(args.fbo_configs)
        except ValueError as exc:
            parser.error(str(exc))
    default_tfs = "4h,1h" if args.strategy == "fbo" else "1h,4h"
    timeframes = [t for t in (args.timeframes or default_tfs).split(",") if t]
    unknown = [t for t in timeframes if t not in REPLAY_TIMEFRAMES]
    if unknown:
        parser.error(f"--timeframes: неизвестные {unknown}, доступны {REPLAY_TIMEFRAMES}")
    return asyncio.run(run(
        args.months, [s for s in args.symbols.split(",") if s], args.cache, args.json_path,
        strategy=args.strategy, timeframes=timeframes, end=args.end,
        fbo_configs=args.fbo_configs,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
