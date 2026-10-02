"""Тесты чистой логики фоновых задач (этап 12) — без сети и без БД.

Проверяются: доля пути до TP/SL и чтение per-user настроек уведомлений.
"""

from __future__ import annotations

from decimal import Decimal

from app.database.models.user import DEFAULT_NOTIFICATIONS, UserSettings
from app.workers.notifier import approach_enabled, notification_enabled
from app.workers.positions import progress_fraction

D = Decimal


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
