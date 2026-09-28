"""Источник цели READY-сигнала (28.09, блок B): уровень или 2R по формуле.

Детекторы ставят Signal.target_source, уведомление сканера пишет
«Цель: уровень X» / «Цель: 2R по формуле — X». build_fingerprint источник
не видит — тест-замок ниже."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.analysis import setups
from app.analysis.setups import BreakoutRetest, EMAPullback, round_price
from app.analysis.signals import Signal, target_line
from app.analysis.structure import Level
from app.trading.enums import SignalDirection, SignalLevel, TargetSource
from app.workers.scanner import build_fingerprint, render_detail
from tests.test_setups import breakout_retest_chart, build_context, pullback_context

D = Decimal


def _detect(which: str) -> Signal:
    if which == "breakout":
        return BreakoutRetest().detect(build_context(breakout_retest_chart()))
    return EMAPullback().detect(pullback_context())


def _entry(signal: Signal) -> Decimal:
    """Вход детектора — край зоны, противоположный уровню/EMA50: для LONG
    это верх зоны (entry = max(entry, level))."""
    assert signal.entry_zone_high is not None
    return signal.entry_zone_high


@pytest.mark.parametrize("which", ["breakout", "pullback"])
def test_no_level_ahead_gives_formula_2r(monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    monkeypatch.setattr(setups, "nearest_level", lambda *a, **kw: None)
    signal = _detect(which)
    assert signal.direction is SignalDirection.LONG, signal.note
    assert signal.target_source is TargetSource.FORMULA_2R
    assert signal.stop_loss is not None
    risk = _entry(signal) - signal.stop_loss
    assert signal.take_profit_1 == round_price(_entry(signal) + risk * 2)
    assert signal.note == "Цель: цель 2R (уровней впереди нет)"


@pytest.mark.parametrize("which", ["breakout", "pullback"])
def test_level_ahead_gives_level(monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    far = Level(
        price=D("260"), touches=3, last_touch_index=0, is_resistance=True, strength=D("1")
    )
    monkeypatch.setattr(setups, "nearest_level", lambda *a, **kw: far)
    signal = _detect(which)
    assert signal.direction is SignalDirection.LONG, signal.note
    assert signal.target_source is TargetSource.LEVEL
    assert signal.take_profit_1 == round_price(D("260"))
    assert signal.note == "Цель: следующий уровень 260.0000"


def test_wait_signal_has_no_target_source() -> None:
    signal = setups.wait_signal("BTC-USDT", "4h", "нет")
    assert signal.target_source is None


class TestTargetLine:
    def test_level(self) -> None:
        assert target_line("126.077", TargetSource.LEVEL) == "Цель: уровень 126.077"

    def test_formula(self) -> None:
        assert (
            target_line("126.077", TargetSource.FORMULA_2R)
            == "Цель: 2R по формуле — 126.077"
        )

    def test_unknown_source_keeps_old_line(self) -> None:
        assert target_line("126.077", None) == "Цель: 126.077"


def _ready(**overrides: object) -> Signal:
    fields: dict[str, object] = {
        "symbol": "SOL-USDT", "timeframe": "4h", "direction": SignalDirection.LONG,
        "setup": "Пробой с ретестом", "entry_zone_low": D("122.5"),
        "entry_zone_high": D("123"), "stop_loss": D("121.646"),
        "take_profit_1": D("126.077"), "risk_reward": D("2.4"), "confidence": 7,
    }
    fields.update(overrides)
    return Signal(**fields)  # type: ignore[arg-type]


def test_render_detail_shows_source() -> None:
    level = render_detail(_ready(target_source=TargetSource.LEVEL), SignalLevel.READY, 3)
    formula = render_detail(_ready(target_source=TargetSource.FORMULA_2R), SignalLevel.READY, 3)
    assert "Цель: уровень 126.077\n" in level
    assert "Цель: 2R по формуле — 126.077\n" in formula


def test_fingerprint_ignores_target_source() -> None:
    """ЗАМОК: источник цели не делает сетап другим — те же уровни дают тот
    же fingerprint, иначе дедуп READY после деплоя разослал бы повторные
    уведомления по уже присланным сетапам."""
    base = _ready()
    fingerprints = {
        build_fingerprint(replace(base, target_source=source), SignalLevel.READY)
        for source in (None, TargetSource.LEVEL, TargetSource.FORMULA_2R)
    }
    assert fingerprints == {build_fingerprint(base, SignalLevel.READY)}


# --- 28.09: строка «Объём пробоя» в READY-уведомлении -----------------------


def _plain_ready() -> Signal:
    return Signal(
        symbol="BTC-USDT", timeframe="4h", direction=SignalDirection.LONG,
        setup="Пробой с ретестом", entry_zone_low=D("100"), entry_zone_high=D("101"),
        stop_loss=D("98"), take_profit_1=D("106"), risk_reward=D("2.5"), confidence=7,
    )


def test_render_detail_ready_shows_breakout_volume_line() -> None:
    signal = replace(_plain_ready(), breakout_volume_ratio=D("1.1234"))
    text = render_detail(signal, SignalLevel.READY)
    assert "\nОбъём пробоя ×1.12 (порог 1.3)\n" in text


def test_lock_render_detail_without_breakout_volume_unchanged() -> None:
    """Откат к EMA50 и старые сигналы — строки нет, текст прежний."""
    text = render_detail(_plain_ready(), SignalLevel.READY)
    assert "Объём пробоя" not in text
    assert "/10\n\n<i>Проверь актуальность" in text
