"""Замки поведения детекторов — 28.09, условие «Объём пробоя».

Условие объёма пробойной свечи добавлено в карточку как информационное:
какие сигналы READY, с какими ценами и какой WAIT выбирает движок — не
меняется. Эталон снят на коде ДО правки (876b055) и зашит в tests/fixtures/setups_lock_golden.json:
по каждому сценарию и каждому детектору (плюс engine.evaluate) — уровень
classify_signal, направление, цены, RR, confidence, note, а у не-READY ещё
и список (имя, passed) условий — по нему движок выбирает WAIT, и по нему
же classify_signal отличает FORMING.

У READY список условий в замок не входит: там и есть новая строка.

Если замок упал — поведение детектора изменилось. Это не «обнови эталон»,
а отдельное решение.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.analysis.classify import classify_signal
from app.analysis.engine import AnalysisEngine
from app.analysis.setups import BreakoutRetest, EMAPullback
from app.analysis.signals import MarketContext, Signal
from app.exchanges.base import Kline
from app.trading.enums import MarketStructure, SignalLevel
from tests.test_setups import (
    breakout_retest_chart,
    build_context,
    candle,
    mirrored,
    pullback_context,
    retest_chart_with_high_confirmation,
)

D = Decimal


def _uptrend_range(breakout_volume: float) -> list[Kline]:
    """breakout_retest_chart, но с заданным объёмом пробойной свечи."""
    chart = breakout_retest_chart()
    i = 216  # индекс пробойной свечи в breakout_retest_chart
    b = chart[i]
    chart[i] = candle(
        i, float(b.open), float(b.high), float(b.low), float(b.close),
        volume=breakout_volume,
    )
    return chart


def _false_breakout() -> list[Kline]:
    return [
        *breakout_retest_chart()[:217],
        candle(217, 205.5, 206, 196, 197, volume=2000),
        candle(218, 197, 201, 196, 200.2),
        candle(219, 200, 203, 199, 202.5, volume=2000),
    ]


def _downtrend() -> list[Kline]:
    out: list[Kline] = []
    price = 300.0
    for i in range(250):
        nxt = price - 0.6
        out.append(candle(i, price, price + 0.5, nxt - 0.5, nxt))
        price = nxt
    return out


def _ctx(candles: list[Kline]) -> Callable[[], MarketContext]:
    return lambda: build_context(candles)


SCENARIOS: dict[str, Callable[[], MarketContext]] = {
    "full": _ctx(breakout_retest_chart()),
    "full_mirror": _ctx(mirrored(breakout_retest_chart())),
    "high_conf_203": _ctx(retest_chart_with_high_confirmation(203.4)),
    "high_conf_203_mirror": _ctx(mirrored(retest_chart_with_high_confirmation(203.4))),
    "high_conf_212": _ctx(retest_chart_with_high_confirmation(212.5)),
    "high_conf_212_mirror": _ctx(mirrored(retest_chart_with_high_confirmation(212.5))),
    "at_breakout": _ctx(breakout_retest_chart()[:-3]),
    "no_confirmation": _ctx(breakout_retest_chart()[:-1]),
    "no_confirmation_mirror": _ctx(mirrored(breakout_retest_chart()[:-1])),
    "false_breakout": _ctx(_false_breakout()),
    "flat": _ctx([candle(i, 100, 100.5, 99.5, 100) for i in range(250)]),
    "downtrend": _ctx(_downtrend()),
    # Пробой на обычном объёме: ×1 от среднего — ниже порога 1.3.
    "low_volume_breakout": _ctx(_uptrend_range(1000)),
    "low_volume_breakout_mirror": _ctx(mirrored(_uptrend_range(1000))),
    "pullback": lambda: pullback_context(),
    "pullback_mirror": lambda: pullback_context(mirror=True),
    # Откат, где пробой тоже ждёт подтверждения: FORMING от одного и WAIT от
    # другого — выбор движка по числу выполненных условий.
    "pullback_ranging": lambda: replace(
        pullback_context(), structure=MarketStructure.RANGE
    ),
}
# Префиксы полного графика: сетап созревает по свечам.
for _k in range(205, 222):
    SCENARIOS[f"prefix_{_k}"] = _ctx(breakout_retest_chart()[:_k])

DETECTORS: dict[str, Callable[[MarketContext], Signal]] = {
    "breakout": BreakoutRetest().detect,
    "pullback": EMAPullback().detect,
    "engine": AnalysisEngine(market=None).evaluate,  # type: ignore[arg-type]
}


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def signature(signal: Signal) -> dict[str, object]:
    level = classify_signal(signal)
    sig: dict[str, object] = {
        "level": level.value if level else None,
        "direction": signal.direction.value,
        "setup": signal.setup,
        "zone": [_s(signal.entry_zone_low), _s(signal.entry_zone_high)],
        "stop": _s(signal.stop_loss),
        "tp": _s(signal.take_profit_1),
        "rr": _s(signal.risk_reward),
        "confidence": signal.confidence,
        "note": signal.note,
    }
    if level is not SignalLevel.READY:
        sig["conditions"] = [[c.name, c.passed] for c in signal.conditions]
    return sig


def current() -> dict[str, dict[str, object]]:
    return {
        f"{scenario}/{detector}": signature(detect(build()))
        for scenario, build in SCENARIOS.items()
        for detector, detect in DETECTORS.items()
    }


GOLDEN_PATH = Path(__file__).parent / "fixtures" / "setups_lock_golden.json"
GOLDEN: dict[str, dict[str, object]] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_lock_detector_behavior_unchanged() -> None:
    """READY/FORMING/WAIT, цены, RR, confidence и выбор движка — как до
    условия «Объём пробоя»."""
    actual = current()
    assert set(actual) == set(GOLDEN)
    diff = {k: (GOLDEN[k], actual[k]) for k in GOLDEN if GOLDEN[k] != actual[k]}
    assert not diff, diff


def test_lock_golden_covers_all_levels() -> None:
    """Эталон не вырожден: в нём есть и READY обоих детекторов, и FORMING,
    и пустой уровень — иначе замок ничего не держит."""
    levels = {(k.split("/")[1], v["level"]) for k, v in GOLDEN.items()}
    assert ("breakout", "READY") in levels
    assert ("pullback", "READY") in levels
    assert ("breakout", "FORMING") in levels
    assert ("engine", None) in levels


@pytest.mark.parametrize("scenario", ["low_volume_breakout", "low_volume_breakout_mirror"])
def test_lock_low_breakout_volume_still_ready(scenario: str) -> None:
    """Объём пробоя не фильтр: пробой на обычном объёме — по-прежнему READY."""
    signal = BreakoutRetest().detect(SCENARIOS[scenario]())
    assert classify_signal(signal) is SignalLevel.READY, signal.note


# --- замок build_fingerprint --------------------------------------------------
# Хэши сняты на коде до признаков (876b055). Признаки сигнала и строка
# «Объём пробоя» в render_detail в отпечаток не входят: иначе каждый
# деплой заново уведомил бы обо всех активных READY, а «Да» по старой
# кнопке отказывало бы SIGNAL_SUPERSEDED.
FINGERPRINT_GOLDEN = {
    "full": "e4edfc86ab4522d862383d3e3fbe368c64ba7ac8f907ebdd5605b1a53e10bc8f",
    "full_mirror": "d9a53d8c7501d60e81177b6d25047db530ba543d5a9731025c019a4d240744bf",
}


@pytest.mark.parametrize("scenario", sorted(FINGERPRINT_GOLDEN))
def test_lock_fingerprint_unchanged(scenario: str) -> None:
    from app.workers.scanner import build_fingerprint

    signal = BreakoutRetest().detect(SCENARIOS[scenario]())
    assert build_fingerprint(signal, SignalLevel.READY) == FINGERPRINT_GOLDEN[scenario]


def test_lock_fingerprint_ignores_features() -> None:
    from datetime import UTC, datetime

    from app.workers.scanner import build_fingerprint

    base = BreakoutRetest().detect(SCENARIOS["full"]())
    variants = [
        replace(
            base,
            breakout_volume_ratio=ratio,
            breakout_at=at,
            ema50_distance_atr=distance,
            conditions=[],
        )
        for ratio, at, distance in [
            (None, None, None),
            (D("0.5"), datetime(2020, 1, 1, tzinfo=UTC), D("0.1")),
            (D("9"), None, D("3")),
        ]
    ]
    fingerprints = {build_fingerprint(v, SignalLevel.READY) for v in variants}
    assert fingerprints == {FINGERPRINT_GOLDEN["full"]}
