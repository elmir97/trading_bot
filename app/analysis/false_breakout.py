"""Ложный пробой по тренду (FBO) — отдельный детектор, в сканер не подключён.

Обоснование владельца 01.10: отрицательная автокорреляция крипты на 1–4h
(сильнее после больших движений); пробои диапазона после комиссий на
низких частотах незначимы — это же показал replay 12 месяцев.

Тренд — обязательное правило: закрытие свечи сигнала ниже EMA200 D1 —
только SHORT (прокол уровня вверх и возврат под него), выше — только LONG
(прокол вниз и возврат над ним). Нет EMA200 D1 — сигнала нет.

Механика SHORT (LONG зеркально):
- уровень — find_levels (правила BreakoutRetest, ≥ 2 касаний) по свечам
  ДО прокола, ATR — тоже до прокола: прокол не может сам нарисовать уровень,
  которым потом подтверждается;
- перед проколом закрытие под уровнем — уровень был сопротивлением;
- прокол: high ≥ уровень + d·ATR;
- возврат: первое после прокола закрытие под уровнем, не позже N свечей;
  свеча прокола сама может быть свечой возврата (прокол тенью);
- вход — закрытие свечи возврата (с confirm_next — закрытие следующей
  свечи, если и она под уровнем); стоп — максимум high от прокола до входа
  + 0.2·ATR; тейк — ближайший уровень за входом, если RR ≥ 1.5, иначе 2R.

Сигнал выдаётся только на свече входа: на следующей свече тот же прокол
уже не «первый возврат».
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.analysis.indicators import atr as calc_atr
from app.analysis.indicators import last_value, volume_ratio
from app.analysis.setups import STOP_BUFFER_ATR, SetupDetector, round_price
from app.analysis.signals import (
    MarketContext,
    Signal,
    SignalCondition,
    validate_geometry,
    wait_signal,
)
from app.analysis.structure import Level, find_levels, nearest_level
from app.exchanges.base import Kline
from app.trading.enums import SignalDirection, TargetSource

ZERO = Decimal(0)
ATR_PERIOD = 14
VOLUME_PERIOD = 20
MIN_TARGET_RR = Decimal("1.5")
FORMULA_RR = Decimal(2)
# Наибольшие N и сдвиг на подтверждение, под которые ищутся события —
# общий проход для всех вариантов детектора.
MAX_RETURN_BARS = 3


@dataclass(frozen=True, slots=True)
class Probe:
    """Прокол уровня и возврат — событие до проверки параметров варианта."""

    direction: SignalDirection
    level: Level
    levels: tuple[Level, ...]   # уровни до прокола — для цели
    atr: Decimal                # ATR до прокола
    probe_index: int
    return_index: int
    excess_atr: Decimal         # насколько прокол зашёл за уровень, в ATR
    volume_ratio: Decimal | None


def _atr_before(candles: list[Kline]) -> Decimal | None:
    if len(candles) <= ATR_PERIOD:
        return None
    value = last_value(calc_atr(
        [c.high for c in candles], [c.low for c in candles], [c.close for c in candles],
        ATR_PERIOD,
    ))
    return value if value is not None and value > ZERO else None


def _volume_ratio_at(candles: list[Kline], index: int) -> Decimal | None:
    volumes = [c.volume for c in candles[: index + 1]]
    if len(volumes) < VOLUME_PERIOD:
        return None
    return volume_ratio(volumes, VOLUME_PERIOD)[-1]


def find_probes(
    candles: list[Kline], direction: SignalDirection, return_index: int
) -> list[Probe]:
    """События «прокол → первый возврат» с возвратом ровно на return_index и
    проколом не раньше return_index − MAX_RETURN_BARS. Будущих свечей нет:
    смотрим только candles[: return_index + 1]."""
    short = direction is SignalDirection.SHORT
    out: list[Probe] = []
    for p in range(return_index, max(return_index - MAX_RETURN_BARS, 1) - 1, -1):
        history = candles[:p]
        atr = _atr_before(history)
        if atr is None:
            continue
        levels = tuple(find_levels(history, atr))
        before = candles[p - 1]
        probe = candles[p]
        for level in levels:
            if short:
                was_below = before.close < level.price
                excess = (probe.high - level.price) / atr
                returned = candles[return_index].close < level.price
                stayed = all(
                    candles[k].close >= level.price for k in range(p, return_index)
                )
            else:
                was_below = before.close > level.price
                excess = (level.price - probe.low) / atr
                returned = candles[return_index].close > level.price
                stayed = all(
                    candles[k].close <= level.price for k in range(p, return_index)
                )
            if was_below and excess > ZERO and returned and stayed:
                out.append(Probe(
                    direction, level, levels, atr, p, return_index, excess,
                    _volume_ratio_at(candles, p),
                ))
    return out


# Один контекст прогоняется через все варианты подряд (replay) — события
# одной свечи считаем один раз. Ключ — сам список свечей контекста по
# идентичности (сильная ссылка: id не переиспользуется), не его поля:
# совпадение времён и цен при другом объёме дало бы чужие события.
_cache_candles: list[Kline] | None = None
_cache_value: dict[tuple[SignalDirection, int], list[Probe]] = {}


def _probes(
    context: MarketContext, direction: SignalDirection, return_index: int
) -> list[Probe]:
    global _cache_candles, _cache_value
    candles = context.candles
    if candles is not _cache_candles:
        _cache_candles, _cache_value = candles, {}
    sub = (direction, return_index)
    if sub not in _cache_value:
        _cache_value[sub] = find_probes(candles, direction, return_index)
    return _cache_value[sub]


class FalseBreakout(SetupDetector):
    name = "Ложный пробой"

    def __init__(
        self,
        *,
        probe_atr: Decimal,
        return_bars: int,
        min_probe_volume_ratio: Decimal | None = None,
        confirm_next: bool = False,
    ) -> None:
        if not 0 <= return_bars <= MAX_RETURN_BARS:
            raise ValueError(f"return_bars {return_bars} вне 0..{MAX_RETURN_BARS}")
        self.probe_atr = probe_atr
        self.return_bars = return_bars
        self.min_probe_volume_ratio = min_probe_volume_ratio
        self.confirm_next = confirm_next

    def detect(self, context: MarketContext) -> Signal:
        candles = context.candles
        if context.d1_ema200 is None or len(candles) < VOLUME_PERIOD + 2:
            return wait_signal(context.symbol, context.timeframe, "Нет EMA200 D1 или истории.")
        entry_index = len(candles) - 1
        entry = candles[entry_index].close
        direction = (
            SignalDirection.SHORT if entry < context.d1_ema200 else SignalDirection.LONG
        )
        short = direction is SignalDirection.SHORT
        return_index = entry_index - 1 if self.confirm_next else entry_index

        chosen: Probe | None = None
        for probe in _probes(context, direction, return_index):
            if return_index - probe.probe_index > self.return_bars:
                continue
            if probe.excess_atr < self.probe_atr:
                continue
            if self.min_probe_volume_ratio is not None and (
                probe.volume_ratio is None or probe.volume_ratio < self.min_probe_volume_ratio
            ):
                continue
            if self.confirm_next and not (
                entry < probe.level.price if short else entry > probe.level.price
            ):
                continue
            # Самый поздний прокол, затем уровень ближе к входу.
            if chosen is None or (probe.probe_index, -abs(probe.level.price - entry)) > (
                chosen.probe_index, -abs(chosen.level.price - entry)
            ):
                chosen = probe
        if chosen is None:
            return wait_signal(context.symbol, context.timeframe, "Ложного пробоя по тренду нет.")
        return self._signal(context, chosen, direction, entry, entry_index)

    def _signal(
        self, context: MarketContext, probe: Probe, direction: SignalDirection,
        entry: Decimal, entry_index: int,
    ) -> Signal:
        candles = context.candles
        short = direction is SignalDirection.SHORT
        span = candles[probe.probe_index: entry_index + 1]
        buffer = probe.atr * STOP_BUFFER_ATR
        stop = round_price(
            max(c.high for c in span) + buffer if short else min(c.low for c in span) - buffer
        )
        risk = abs(entry - stop)
        if risk <= ZERO:
            return wait_signal(context.symbol, context.timeframe, "Нулевой риск.")
        target_level = nearest_level(list(probe.levels), entry, above=not short)
        rr_level = abs(target_level.price - entry) / risk if target_level else None
        if target_level is not None and rr_level is not None and rr_level >= MIN_TARGET_RR:
            target = round_price(target_level.price)
            source = TargetSource.LEVEL
        else:
            target = round_price(entry - risk * FORMULA_RR if short else entry + risk * FORMULA_RR)
            source = TargetSource.FORMULA_2R
        # Зона — вход и уровень; вход — край, дальний от стопа (detector_entry).
        level_price = probe.level.price
        zone_low, zone_high = (
            (entry, round_price(level_price)) if short else (round_price(level_price), entry)
        )
        if problem := validate_geometry(direction, zone_low, zone_high, stop):
            return wait_signal(context.symbol, context.timeframe, problem)
        rr = (abs(target - entry) / risk).quantize(Decimal("0.01"))
        return Signal(
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            setup=self.name,
            entry_zone_low=zone_low,
            entry_zone_high=zone_high,
            stop_loss=stop,
            take_profit_1=target,
            risk_reward=rr,
            level_price=level_price,
            target_source=source,
            probe_at=candles[probe.probe_index].open_time,
            probe_volume_ratio=probe.volume_ratio,
            confidence=5,
            conditions=[
                SignalCondition("Тренд D1", True, f"EMA200 D1 {context.d1_ema200}"),
                SignalCondition(
                    "Прокол", True, f"{probe.excess_atr:.2f} ATR за уровнем {level_price}"
                ),
                SignalCondition(
                    "Возврат", True,
                    f"через {probe.return_index - probe.probe_index} свеч. после прокола",
                ),
            ],
            note=f"Ложный пробой {'вверх' if short else 'вниз'} уровня {level_price}",
        )
