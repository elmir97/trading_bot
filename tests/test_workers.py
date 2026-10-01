"""Тесты чистой логики фоновых задач (этап 12) — без сети и без БД.

Проверяются: доля пути до TP/SL и чтение per-user настроек уведомлений.
"""

from __future__ import annotations

from decimal import Decimal

from app.database.models.user import DEFAULT_NOTIFICATIONS, UserSettings
from app.workers.notifier import notification_enabled
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
