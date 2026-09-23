"""Тесты app/workers/execution_digest.py (этап 15.4, раздел 12а ТЗ).

Чистая логика подсчёта и рендера — без БД и без сети: build_stats() и
detect_anomalies() читают уже готовые ExecutionOrder (role=ENTRY, как их
отдаёт ExecutionOrderRepository.list_entries_between()), собранные тут
вручную, без сессии. Как test_workers.py — те же принципы (Decimal,
никакого мока сети/БД).

ready_signals (счётчик из таблицы signals) сюда же передаётся готовым
числом — build_stats() ничего не знает о SignalRepository, поэтому
никакого мока БД для него не нужно, как и для execution_orders.
"""

from __future__ import annotations

from decimal import Decimal

from app.database.models.execution_order import ExecutionOrder
from app.trading.enums import OrderRole, OrderSide, OrderStatus, OrderType, TradeSide
from app.workers.execution_digest import (
    build_stats,
    detect_anomalies,
    render_execution_digest,
)
from app.workers.scanner import ScanCycleStats

D = Decimal


def _row(status: OrderStatus, **overrides: object) -> ExecutionOrder:
    fields: dict[str, object] = {
        "user_id": 1,
        "symbol": "BTC-USDT",
        "side": OrderSide.BUY,
        "position_side": TradeSide.LONG,
        "order_type": OrderType.MARKET,
        "role": OrderRole.ENTRY,
        "status": status,
    }
    fields.update(overrides)
    return ExecutionOrder(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Подсчёты по подтверждениям и отказам
# ---------------------------------------------------------------------------


class TestBuildStats:
    def test_counts_each_card_outcome_once(self) -> None:
        rows = [
            _row(OrderStatus.DRY_RUN, risk_percent=D("1.0"), risk_reward=D("2.5")),
            _row(OrderStatus.DRY_RUN, risk_percent=D("1.0"), risk_reward=D("2.0")),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.EXPIRED),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.confirmed == 2
        assert stats.declined == 3
        assert stats.expired == 1
        assert stats.total_cards == 6

    def test_refusals_grouped_by_code_not_counted_in_cards(self) -> None:
        rows = [
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="PRICE_DRIFT"),
            _row(OrderStatus.DRY_RUN, risk_percent=D("1.0")),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.refusals_by_code == {"MAX_POSITIONS": 2, "PRICE_DRIFT": 1}
        assert stats.total_refusals == 3
        assert stats.total_cards == 1  # отказы гварда не показывают карточку
        assert stats.total_attempts == 4

    def test_only_confirmed_rows_feed_averages(self) -> None:
        rows = [
            _row(
                OrderStatus.DRY_RUN, risk_percent=D("1.0"), risk_reward=D("2.0"),
                price_drift_percent=D("0.1"),
            ),
            # DECLINED/REFUSED со своими цифрами не должны попасть в средние.
            _row(OrderStatus.DECLINED, risk_percent=D("9.0"), risk_reward=D("9.0")),
            _row(OrderStatus.REFUSED, error_code="X"),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.confirmed_risk_percents == [D("1.0")]
        assert stats.confirmed_risk_rewards == [D("2.0")]
        assert stats.confirmed_drift_percents == [D("0.1")]


# ---------------------------------------------------------------------------
# Аномалии
# ---------------------------------------------------------------------------


class TestAnomalies:
    def test_no_anomalies_on_clean_day(self) -> None:
        rows = [
            _row(
                OrderStatus.DRY_RUN, symbol="BTC-USDT", risk_percent=D("1.95"),
                risk_reward=D("2.2"), price_drift_percent=D("0.12"),
            ),
        ]
        stats = build_stats(rows, target_risk_percent=D("2.0"))
        assert detect_anomalies(stats, max_price_drift_ratio=D("0.3")) == []

    def test_risk_deviation_over_10_percent_flagged(self) -> None:
        # Цель 2.0%, факт 1.7% — отклонение 15% > 10%, но < 50% (не undersized).
        rows = [_row(OrderStatus.DRY_RUN, symbol="ETH-USDT", risk_percent=D("1.7"))]
        stats = build_stats(rows, target_risk_percent=D("2.0"))
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert len(anomalies) == 1
        assert "ETH-USDT" in anomalies[0]
        assert stats.undersized == []

    def test_risk_deviation_within_10_percent_not_flagged(self) -> None:
        # 1.95% при цели 2.0% — отклонение 2.5%, в пределах допустимого.
        rows = [_row(OrderStatus.DRY_RUN, risk_percent=D("1.95"))]
        stats = build_stats(rows, target_risk_percent=D("2.0"))
        assert detect_anomalies(stats, max_price_drift_ratio=D("0.3")) == []

    def test_undersized_lot_flagged_separately_from_deviation(self) -> None:
        # 0.9% при цели 2.0% — отклонение 55% > 50%: и "отклонился", и
        # отдельно "слишком мелкий лот" — два разных пункта из раздела 12а.
        rows = [_row(OrderStatus.DRY_RUN, symbol="BTC-USDT", risk_percent=D("0.9"))]
        stats = build_stats(rows, target_risk_percent=D("2.0"))
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert len(anomalies) == 2
        assert any("меньше половины" in a for a in anomalies)

    def test_average_drift_above_half_threshold_flagged(self) -> None:
        # Допустимый порог 0.3 → в процентах то же самое (30%), половина —
        # 15%. Средний дрейф 20% > 15% — аномалия. Три карточки — на грани
        # PRICE_DRIFT_MIN_CARDS, минимум выполнен.
        rows = [
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("18")),
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("22")),
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("20")),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert any("дрейф" in a for a in anomalies)

    def test_average_drift_below_half_threshold_not_flagged(self) -> None:
        rows = [
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("5")),
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("4")),
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("6")),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert not any("дрейф" in a for a in anomalies)

    def test_average_drift_below_min_cards_not_flagged_even_if_high(self) -> None:
        # Те же 20% среднего дрейфа, что и в flagged-тесте выше, но только
        # 2 карточки < PRICE_DRIFT_MIN_CARDS — правило не должно сработать,
        # сколько бы ни был велик сам дрейф на такой маленькой выборке.
        rows = [
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("18")),
            _row(OrderStatus.DRY_RUN, price_drift_percent=D("22")),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert not any("дрейф" in a for a in anomalies)

    def test_dominant_guard_code_flagged(self) -> None:
        # 3 из 5 попыток отказаны одним и тем же гвардом — больше половины.
        rows = [
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.DRY_RUN, risk_percent=D("1")),
            _row(OrderStatus.DECLINED),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert any("MAX_POSITIONS" in a for a in anomalies)

    def test_evenly_spread_guard_codes_not_flagged(self) -> None:
        # 5 попыток — минимум выполнен, но ни один код не набирает больше
        # половины (2 из 5 максимум у каждого).
        rows = [
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="PRICE_DRIFT"),
            _row(OrderStatus.DRY_RUN, risk_percent=D("1")),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.EXPIRED),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert not any("срабатывает подозрительно часто" in a for a in anomalies)

    def test_guard_dominance_below_min_attempts_not_flagged(self) -> None:
        # Вчерашний кейс: один отказ гварда на одну попытку за сутки — доля
        # 100%, но меньше GUARD_DOMINANCE_MIN_ATTEMPTS, поэтому не аномалия.
        rows = [_row(OrderStatus.REFUSED, error_code="SYMBOL_NOT_ALLOWED")]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.total_attempts == 1
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert anomalies == []

    def test_guard_dominance_at_min_attempts_flagged(self) -> None:
        # Тот же перекос (100% одним кодом), но ровно на пороге в 5 попыток —
        # теперь должен сработать.
        rows = [_row(OrderStatus.REFUSED, error_code="SYMBOL_NOT_ALLOWED") for _ in range(5)]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.total_attempts == 5
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert any("SYMBOL_NOT_ALLOWED" in a for a in anomalies)


# ---------------------------------------------------------------------------
# Рендер, включая нулевой день
# ---------------------------------------------------------------------------


class TestRenderExecutionDigest:
    def test_zero_signals_day_renders_without_errors(self) -> None:
        stats = build_stats([], target_risk_percent=D("2.0"), ready_signals=0)
        text = render_execution_digest(stats, max_price_drift_ratio=D("0.3"))
        assert "Сигналов READY: 0" in text
        assert "показана карточка: 0" in text
        assert "подтверждено: 0" in text
        assert "отказ пользователя: 0" in text
        assert "истекло по TTL: 0" in text
        assert "отказ кода до карточки: 0" in text
        assert "Аномалии: нет" in text
        # Средних строк для пустого дня быть не должно — делить не на что.
        assert "Средний" not in text

    def test_normal_day_renders_counts_and_averages(self) -> None:
        rows = [
            _row(
                OrderStatus.DRY_RUN, risk_percent=D("1.0"), risk_reward=D("2.0"),
                price_drift_percent=D("0.1"),
            ),
            _row(
                OrderStatus.DRY_RUN, risk_percent=D("0.8"), risk_reward=D("2.4"),
                price_drift_percent=D("0.14"),
            ),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.DECLINED),
            _row(OrderStatus.EXPIRED),
            _row(OrderStatus.REFUSED, error_code="MAX_POSITIONS"),
            _row(OrderStatus.REFUSED, error_code="PRICE_DRIFT"),
        ]
        # target_risk_percent=None — эта проверка про форматирование счётчиков
        # и средних, не про раздел "Аномалии" (у него свой TestAnomalies).
        # ready_signals=9 — намеренно не равно total_cards(6) и не равно
        # total_attempts(8): источник другой (signals), сумма карточек и
        # отказов не обязана с ним совпадать.
        stats = build_stats(rows, target_risk_percent=None, ready_signals=9)
        text = render_execution_digest(stats, max_price_drift_ratio=D("0.3"))

        assert "Сигналов READY: 9" in text
        assert "показана карточка: 6" in text
        assert "подтверждено: 2" in text
        assert "отказ пользователя: 3" in text
        assert "истекло по TTL: 1" in text
        assert "отказ кода до карточки: 2" in text
        assert "MAX_POSITIONS — 1" in text
        assert "PRICE_DRIFT — 1" in text
        assert "Средний расчётный риск" in text
        assert "Средний RR" in text
        assert "Средний дрейф цены сигнала к моменту подтверждения" in text
        assert "Аномалии: нет" in text

    def test_guard_refusal_before_card_reflected_in_funnel_not_lost(self) -> None:
        # Вчерашний баг-репорт: 1 READY-сигнал, отказ гварда ещё до карточки
        # (SYMBOL_NOT_ALLOWED), карточка не показана. Раньше "Сигналов READY"
        # считался по total_cards и печатал 0 при реальном сигнале — теперь
        # READY берётся из отдельного счётчика и не занижается отказами.
        rows = [_row(OrderStatus.REFUSED, error_code="SYMBOL_NOT_ALLOWED")]
        stats = build_stats(rows, target_risk_percent=None, ready_signals=1)
        text = render_execution_digest(stats, max_price_drift_ratio=D("0.3"))

        assert "Сигналов READY: 1" in text
        assert "показана карточка: 0" in text
        assert "отказ кода до карточки: 1" in text
        assert "SYMBOL_NOT_ALLOWED — 1" in text
        # Один отказ на одну попытку — доля 100%, но ниже минимума выборки:
        # не должно превращаться в "подозрительно часто".
        assert "Аномалии: нет" in text

    def test_scan_cycle_line_printed_when_given(self) -> None:
        stats = build_stats([], target_risk_percent=None, ready_signals=0)
        scan_cycle = ScanCycleStats(
            symbols_scanned=12, requests_made=34, duration_seconds=5.67
        )
        text = render_execution_digest(
            stats, max_price_drift_ratio=D("0.3"), scan_cycle=scan_cycle
        )
        assert "Скан рынка: 12 символов, 34 запросов, 5.7 с" in text

    def test_scan_cycle_line_absent_when_not_given(self) -> None:
        """Сканер мог не отработать ни разу после рестарта — это не "0",
        строка просто не печатается (не выдумываем данных, которых нет)."""
        stats = build_stats([], target_risk_percent=None, ready_signals=0)
        text = render_execution_digest(stats, max_price_drift_ratio=D("0.3"))
        assert "Скан рынка" not in text


# ---------------------------------------------------------------------------
# Пакет C: стадии отказов и сбои биржи
# ---------------------------------------------------------------------------


def _stage_row(status: OrderStatus, stage: str | None, code: str | None = None) -> ExecutionOrder:
    return _row(status, stage=stage, error_code=code)


class TestStages:
    def test_refusals_split_by_stage(self) -> None:
        rows = [
            _stage_row(OrderStatus.REFUSED, "card", "MAX_POSITIONS"),
            _stage_row(OrderStatus.REFUSED, "confirm", "PRICE_DRIFT"),
            _stage_row(OrderStatus.REFUSED, "confirm", "PRICE_DRIFT"),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.refusals_card_by_code == {"MAX_POSITIONS": 1}
        assert stats.refusals_confirm_by_code == {"PRICE_DRIFT": 2}
        assert stats.refusals_by_code == {"MAX_POSITIONS": 1, "PRICE_DRIFT": 2}

    def test_null_stage_keeps_old_semantics_before_card(self) -> None:
        """Строки до появления колонки — «до карточки», без эвристик: даже
        PRICE_DRIFT (который на карточке отказать не может) без стадии
        остаётся в прежней категории."""
        rows = [_stage_row(OrderStatus.REFUSED, None, "PRICE_DRIFT")]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.refusals_card_by_code == {"PRICE_DRIFT": 1}
        assert stats.refused_confirm == 0

    def test_confirm_stage_refusal_counts_as_shown_card(self) -> None:
        rows = [
            _row(OrderStatus.DRY_RUN),
            _stage_row(OrderStatus.REFUSED, "confirm", "SIGNAL_STALE"),
            _stage_row(OrderStatus.ERROR, "confirm", "ExchangeUnavailableError"),
            _stage_row(OrderStatus.REFUSED, "card", "MAX_POSITIONS"),
            _stage_row(OrderStatus.ERROR, "card", "ExchangeAuthError"),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.total_cards == 3       # DRY_RUN + отказ на «Да» + сбой на «Да»
        assert stats.refused_card == 1
        assert stats.errors_card == 1
        assert stats.total_attempts == 5    # одна строка — одна попытка

    def test_errors_counted_by_stage_and_class(self) -> None:
        rows = [
            _stage_row(OrderStatus.ERROR, "card", "ExchangeAuthError"),
            _stage_row(OrderStatus.ERROR, "confirm", "ExchangeAuthError"),
            _stage_row(OrderStatus.ERROR, None, "ExchangeUnavailableError"),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert (stats.errors_card, stats.errors_confirm) == (2, 1)
        assert stats.errors_by_code == {"ExchangeAuthError": 2, "ExchangeUnavailableError": 1}
        assert stats.refusals_by_code == {}

    def test_error_rows_do_not_leak_into_averages(self) -> None:
        rows = [_row(OrderStatus.ERROR, risk_percent=D("5"), risk_reward=D("9"))]
        stats = build_stats(rows, target_risk_percent=D("1"))
        assert stats.confirmed_risk_percents == []
        assert stats.risk_deviations == []


class TestErrorAnomalies:
    def test_any_exchange_error_is_an_anomaly(self) -> None:
        rows = [_stage_row(OrderStatus.ERROR, "card", "ExchangeAuthError")]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert anomalies == ["сбои биржи при попытках входа: 1 из 1 (ExchangeAuthError — 1)"]

    def test_no_errors_no_anomaly(self) -> None:
        stats = build_stats([_row(OrderStatus.DRY_RUN)], target_risk_percent=None)
        assert detect_anomalies(stats, max_price_drift_ratio=D("0.3")) == []

    def test_errors_are_excluded_from_guard_dominance_denominator(self) -> None:
        """4 отказа MAX_POSITIONS, 4 сбоя биржи, 1 подтверждение: со сбоями в
        знаменателе было бы 4 из 9 (меньше половины) — правило молчало бы
        ровно во время сбоя биржи. Без них — 4 из 5."""
        rows = [_stage_row(OrderStatus.REFUSED, "card", "MAX_POSITIONS") for _ in range(4)]
        rows += [_stage_row(OrderStatus.ERROR, "card", "ExchangeAuthError") for _ in range(4)]
        rows += [_stage_row(OrderStatus.DRY_RUN, None)]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.total_attempts == 9
        assert stats.guard_attempts == 5
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert "гвард MAX_POSITIONS срабатывает подозрительно часто: 4 из 5 попыток" in anomalies

    def test_guard_rule_sums_both_stages(self) -> None:
        rows = [
            _stage_row(OrderStatus.REFUSED, "card", "SIGNAL_STALE"),
            _stage_row(OrderStatus.REFUSED, "card", "SIGNAL_STALE"),
            _stage_row(OrderStatus.REFUSED, "confirm", "SIGNAL_STALE"),
            _stage_row(OrderStatus.REFUSED, "confirm", "SIGNAL_STALE"),
            _row(OrderStatus.DRY_RUN),
            _row(OrderStatus.DECLINED),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert any("SIGNAL_STALE" in a and "4 из 6" in a for a in anomalies)

    def test_denominator_still_equals_row_count_without_errors(self) -> None:
        """Инвариант: без ERROR-строк знаменатель правила такой же, как до
        пакета C (число строк), — разделение стадий его не меняет."""
        rows = [
            _row(OrderStatus.DRY_RUN),
            _stage_row(OrderStatus.REFUSED, "card", "A"),
            _stage_row(OrderStatus.REFUSED, "confirm", "B"),
            _stage_row(OrderStatus.REFUSED, None, "C"),
            _row(OrderStatus.EXPIRED),
        ]
        stats = build_stats(rows, target_risk_percent=None)
        assert stats.guard_attempts == stats.total_attempts == len(rows)


class TestFunnelRender:
    def _text(self, rows: list[ExecutionOrder], ready: int = 0) -> str:
        stats = build_stats(rows, target_risk_percent=None, ready_signals=ready)
        return render_execution_digest(stats, max_price_drift_ratio=D("0.3"))

    def test_funnel_lines_printed_even_when_zero(self) -> None:
        text = self._text([])
        for line in (
            "Сигналов READY: 0",
            "  показана карточка: 0",
            "    подтверждено: 0",
            "    отказ пользователя: 0",
            "    истекло по TTL: 0",
            "    отказ кода при подтверждении: 0",
            "    сбой биржи при подтверждении: 0",
            "  отказ кода до карточки: 0",
            "  сбой биржи до карточки: 0",
        ):
            assert line in text.splitlines(), line

    def test_code_breakdown_only_for_nonzero(self) -> None:
        rows = [
            _stage_row(OrderStatus.REFUSED, "confirm", "PRICE_DRIFT"),
            _stage_row(OrderStatus.REFUSED, "card", "MAX_POSITIONS"),
            _stage_row(OrderStatus.REFUSED, "card", "MAX_POSITIONS"),
        ]
        lines = self._text(rows, ready=3).splitlines()
        i = lines.index("    отказ кода при подтверждении: 1")
        assert lines[i + 1] == "      PRICE_DRIFT — 1"
        j = lines.index("  отказ кода до карточки: 2")
        assert lines[j + 1] == "    MAX_POSITIONS — 2"
        assert not any(" — 0" in line for line in lines)

    def test_layout_order_matches_spec(self) -> None:
        rows = [_row(OrderStatus.DRY_RUN)]
        lines = self._text(rows, ready=1).splitlines()
        order = [
            "Сигналов READY: 1",
            "  показана карточка: 1",
            "    подтверждено: 1",
            "    отказ пользователя: 0",
            "    истекло по TTL: 0",
            "    отказ кода при подтверждении: 0",
            "    сбой биржи при подтверждении: 0",
            "  отказ кода до карточки: 0",
            "  сбой биржи до карточки: 0",
        ]
        idx = [lines.index(x) for x in order]
        assert idx == sorted(idx)

    def test_error_anomaly_is_rendered(self) -> None:
        text = self._text([_stage_row(OrderStatus.ERROR, "confirm", "ExchangeAuthError")])
        assert "сбои биржи при попытках входа: 1 из 1 (ExchangeAuthError — 1)" in text
        assert "Аномалии: нет" not in text



class TestRealSubmissionStatuses:
    """Шаг 15.5.2а: исходы реальной отправки в разборе статусов."""

    def test_counted_as_shown_cards(self) -> None:
        rows = [
            _row(OrderStatus.SUBMITTED),
            _row(OrderStatus.REJECTED),
            _row(OrderStatus.UNKNOWN),
            _row(OrderStatus.PENDING),
            _row(OrderStatus.DRY_RUN, risk_percent=D("1")),
        ]
        stats = build_stats(rows, target_risk_percent=D("1"))
        assert (stats.submitted, stats.rejected, stats.unknown, stats.pending) == (1, 1, 1, 1)
        assert stats.total_cards == 5
        assert stats.total_attempts == 5

    def test_unknown_outcome_is_an_anomaly(self) -> None:
        rows = [_row(OrderStatus.UNKNOWN), _row(OrderStatus.PENDING), _row(OrderStatus.SUBMITTED)]
        stats = build_stats(rows, target_risk_percent=D("1"))
        anomalies = detect_anomalies(stats, max_price_drift_ratio=D("0.3"))
        assert (
            "исход отправки неизвестен: 2 (UNKNOWN — 1, PENDING — 1) — сверить позиции в BingX"
            in anomalies
        )

    def test_submitted_only_is_not_an_anomaly(self) -> None:
        stats = build_stats([_row(OrderStatus.SUBMITTED)], target_risk_percent=D("1"))
        assert detect_anomalies(stats, max_price_drift_ratio=D("0.3")) == []
