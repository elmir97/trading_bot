"""Тесты чистой логики фоновых задач (этап 12) — без сети и без БД.

Проверяются: классификация READY/FORMING по Signal (граница — единственное
невыполненное условие "Подтверждающий паттерн"), fingerprint для дедупа
уведомлений, доля пути до TP/SL и чтение per-user настроек уведомлений.
"""

from __future__ import annotations

from decimal import Decimal

from app.analysis.signals import Signal, SignalCondition
from app.database.models.user import DEFAULT_NOTIFICATIONS, UserSettings
from app.trading.enums import SignalDirection, SignalLevel
from app.workers.notifier import notification_enabled
from app.workers.positions import progress_fraction
from app.workers.scanner import build_fingerprint, classify_signal, render_detail

D = Decimal


def _condition(name: str, passed: bool) -> SignalCondition:
    return SignalCondition(name=name, passed=passed, detail="")


def _ready_signal(**overrides) -> Signal:
    defaults = dict(
        symbol="BTC-USDT",
        timeframe="4h",
        direction=SignalDirection.LONG,
        setup="Пробой с ретестом",
        entry_zone_low=D("100"),
        entry_zone_high=D("101"),
        stop_loss=D("98"),
        take_profit_1=D("106"),
        risk_reward=D("2.5"),
        confidence=7,
        conditions=[_condition("Фильтр EMA200", True)],
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _wait_signal(conditions: list[SignalCondition], note: str = "ждём подтверждения") -> Signal:
    # setup у WAIT-сигналов всегда "Нет сетапа" в реальном коде
    # (app.analysis.signals.wait_signal) — намеренно не переопределяем его
    # здесь, чтобы тесты ловили регрессию, если build_fingerprint/render_detail
    # снова начнут полагаться на setup для FORMING.
    return Signal(
        symbol="BTC-USDT",
        timeframe="4h",
        direction=SignalDirection.WAIT,
        setup="Нет сетапа",
        note=note,
        conditions=conditions,
    )


class TestClassifySignal:
    def test_actionable_signal_is_ready(self) -> None:
        assert classify_signal(_ready_signal()) is SignalLevel.READY

    def test_only_confirmation_missing_is_forming(self) -> None:
        signal = _wait_signal(
            [
                _condition("Фильтр EMA200", True),
                _condition("Пробой уровня", True),
                _condition("Подтверждающий паттерн", False),
            ]
        )
        assert classify_signal(signal) is SignalLevel.FORMING

    def test_multiple_failed_conditions_is_neither(self) -> None:
        """Не хватает не только подтверждения — рано считать даже FORMING."""
        signal = _wait_signal(
            [
                _condition("Фильтр EMA200", True),
                _condition("Пробой уровня", False),
                _condition("Подтверждающий паттерн", False),
            ]
        )
        assert classify_signal(signal) is None

    def test_different_failed_condition_is_neither(self) -> None:
        signal = _wait_signal(
            [
                _condition("Фильтр EMA200", True),
                _condition("Пробой уровня", False),
            ]
        )
        assert classify_signal(signal) is None

    def test_no_conditions_is_neither(self) -> None:
        assert classify_signal(_wait_signal([])) is None


class TestFingerprint:
    def test_stable_for_identical_ready_signal(self) -> None:
        a = build_fingerprint(_ready_signal(), SignalLevel.READY)
        b = build_fingerprint(_ready_signal(), SignalLevel.READY)
        assert a == b

    def test_changes_when_entry_moves(self) -> None:
        a = build_fingerprint(_ready_signal(), SignalLevel.READY)
        b = build_fingerprint(
            _ready_signal(entry_zone_low=D("103"), entry_zone_high=D("104")),
            SignalLevel.READY,
        )
        assert a != b

    def test_forming_ignores_price_wobble(self) -> None:
        """FORMING хэшируется грубее: мелкие колебания не должны считаться
        другим сетапом и плодить повторные уведомления каждый скан."""
        signal_a = _wait_signal([_condition("Подтверждающий паттерн", False)])
        signal_b = _wait_signal([_condition("Подтверждающий паттерн", False)])
        assert (
            build_fingerprint(signal_a, SignalLevel.FORMING)
            == build_fingerprint(signal_b, SignalLevel.FORMING)
        )

    def test_forming_uses_note_not_setup(self) -> None:
        """signal.setup у WAIT всегда "Нет сетапа" в реальном коде — дедуп
        FORMING обязан различать сценарии по note, иначе разные детекторы
        схлопнутся в один и тот же fingerprint."""
        a = _wait_signal(
            [_condition("Подтверждающий паттерн", False)],
            note="Цена на ретесте, но подтверждения нет.",
        )
        b = _wait_signal(
            [_condition("Подтверждающий паттерн", False)],
            note="Цена у EMA50, но подтверждения нет.",
        )
        assert (
            build_fingerprint(a, SignalLevel.FORMING)
            != build_fingerprint(b, SignalLevel.FORMING)
        )

    def test_forming_detail_does_not_leak_generic_setup_label(self) -> None:
        """Регрессия: render_detail раньше показывал "Формируется сетап:
        Нет сетапа" — signal.setup у WAIT ничего не говорит о сценарии."""
        signal = _wait_signal(
            [_condition("Подтверждающий паттерн", False)],
            note="Цена на ретесте, но подтверждения нет.",
        )
        detail = render_detail(signal, SignalLevel.FORMING)
        assert "Нет сетапа" not in detail
        assert "Цена на ретесте" in detail


class TestProgressFraction:
    def test_long_take_profit_progress(self) -> None:
        # Вход 100, цель 110, цена 109 — пройдено 90% пути.
        progress = progress_fraction(D("100"), D("110"), D("109"))
        assert progress == D("0.9")

    def test_short_like_target_below_entry(self) -> None:
        # Стоп-лосс лонга (или тейк шорта): цель ниже входа — знак сам
        # отражает направление, отдельно сторону сделки передавать не нужно.
        progress = progress_fraction(D("100"), D("90"), D("91"))
        assert progress == D("0.9")

    def test_zero_distance_is_none(self) -> None:
        assert progress_fraction(D("100"), D("100"), D("100")) is None

    def test_price_at_entry_is_zero_progress(self) -> None:
        assert progress_fraction(D("100"), D("110"), D("100")) == D("0")


class TestNotificationEnabled:
    def test_missing_settings_falls_back_to_default(self) -> None:
        assert notification_enabled(None, "setup_ready") is True

    def test_reads_explicit_false(self) -> None:
        settings = UserSettings(notifications={"setup_ready": False})
        assert notification_enabled(settings, "setup_ready") is False

    def test_missing_key_falls_back_to_default_not_false(self) -> None:
        """Старый пользователь без нового ключа в JSONB — включено по
        умолчанию, а не выключено молча."""
        settings = UserSettings(notifications={})
        assert notification_enabled(settings, "setup_forming") is True

    def test_default_notifications_has_all_stage12_keys(self) -> None:
        for key in ("setup_ready", "setup_forming", "setup_charts", "tp_sl_approaching"):
            assert DEFAULT_NOTIFICATIONS[key] is True
        assert "setup_found" not in DEFAULT_NOTIFICATIONS
