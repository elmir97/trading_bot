"""Тесты чистой логики фоновых задач (этап 12) — без сети и без БД.

Проверяются: расстояние до SL/TP (этап 5) и чтение per-user настроек уведомлений.
"""

from __future__ import annotations

from decimal import Decimal

from app.database.models.user import DEFAULT_NOTIFICATIONS, UserSettings
from app.trading.enums import TradeSide
from app.workers.notifier import approach_enabled, notification_enabled
from app.workers.positions import approach_geometry, risk_unit

D = Decimal


G = approach_geometry
L, S = TradeSide.LONG, TradeSide.SHORT


class TestApproachGeometry:
    """(осталось, база) — расстояние до уровня в сторону срабатывания."""

    def test_natural_levels_match_path_share(self) -> None:
        # LONG стоп 90 при входе 100, mark 92: осталось 2 из пути 10 (80%).
        assert G(L, "SL", D(100), D(90), D(92), None) == (D(2), D(10))
        # LONG тейк 110, mark 109: осталось 1 из 10.
        assert G(L, "TP", D(100), D(110), D(109), None) == (D(1), D(10))
        # SHORT стоп 110, mark 108; SHORT тейк 90, mark 91.
        assert G(S, "SL", D(100), D(110), D(108), None) == (D(2), D(10))
        assert G(S, "TP", D(100), D(90), D(91), None) == (D(1), D(10))

    def test_short_breakeven_stop_live_case(self) -> None:
        """02.10 18:00:48: SHORT вход 1.5069, стоп 1.5053, mark 1.5002 — до
        стопа 0.0051 вверх, база без R — 1% входа; было «пройдено 418.8%»."""
        remaining, base = G(
            S, "SL", D("1.5069"), D("1.5053"), D("1.5002"), None
        )
        assert remaining == D("0.0051") and base == D("0.015069")

    def test_flipped_level_uses_r_then_fallback(self) -> None:
        # LONG стоп выше входа (в прибыли): база — 1R журнала.
        assert G(L, "SL", D(100), D(101), D(103), D(10)) == (D(2), D(10))
        # Без R — max(|вход − уровень|, 1% входа).
        assert G(L, "SL", D(100), D(103), D(104), None)[1] == D(3)
        assert G(L, "SL", D(100), D("100.2"), D(101), None)[1] == D(1)

    def test_passed_level_is_not_positive(self) -> None:
        assert G(L, "SL", D(100), D(90), D(89), None)[0] < 0

    def test_risk_unit_only_from_loss_side_stop(self) -> None:
        assert risk_unit(TradeSide.LONG, D(100), D(90)) == D(10)
        assert risk_unit(TradeSide.SHORT, D(100), D(110)) == D(10)
        assert risk_unit(TradeSide.SHORT, D("1.5069"), D("1.5053")) is None   # #11: безубыток
        assert risk_unit(TradeSide.LONG, D(100), None) is None


class TestNotificationEnabled:
    def test_missing_settings_falls_back_to_default(self) -> None:
        assert notification_enabled(None, "daily_report") is True

    def test_reads_explicit_false(self) -> None:
        settings = UserSettings(notifications={"daily_report": False})
        assert notification_enabled(settings, "daily_report") is False

    def test_missing_key_falls_back_to_default_not_false(self) -> None:
        """Старый пользователь без нового ключа в JSONB — включено по
        умолчанию, а не выключено молча."""
        settings = UserSettings(notifications={})
        assert notification_enabled(settings, "daily_report") is True

    def test_approach_new_keys_then_legacy_then_default(self) -> None:
        """Этап 5: sl_/tp_approaching раздельно; без них — старый общий
        tp_sl_approaching (M2 данные не переносит); без него — включено."""
        assert approach_enabled(None, "sl_approaching") is True
        legacy_off = UserSettings(notifications={"tp_sl_approaching": False})
        assert approach_enabled(legacy_off, "sl_approaching") is False
        assert approach_enabled(legacy_off, "tp_approaching") is False
        split = UserSettings(notifications={"tp_sl_approaching": False, "tp_approaching": True})
        assert approach_enabled(split, "tp_approaching") is True
        assert approach_enabled(split, "sl_approaching") is False

    def test_default_notifications_keep_monitor_and_drop_signal_keys(self) -> None:
        assert DEFAULT_NOTIFICATIONS["sl_approaching"] is True
        assert DEFAULT_NOTIFICATIONS["tp_approaching"] is True
        assert "tp_sl_approaching" not in DEFAULT_NOTIFICATIONS
        # Сигналы удалены 02.10.2026 — их переключателей в дефолтах нет.
        for key in ("setup_found", "setup_ready", "setup_forming", "setup_charts"):
            assert key not in DEFAULT_NOTIFICATIONS
