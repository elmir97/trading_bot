"""scripts/signal_outcomes.py — чистая логика отчёта исходов сигналов.

Сеть и прод не участвуют: свечи строятся вручную, клиент биржи — фейк.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.analysis.engine import CANDLES_REQUIRED
from app.analysis.setups import BreakoutRetest, EMAPullback
from app.exchanges.base import Kline
from app.exchanges.bingx import QUOTE_KLINES
from app.trading.enums import SignalDirection
from scripts import signal_outcomes as so
from tests.test_setups import breakout_retest_chart, build_context

D = Decimal
T0 = datetime(2026, 9, 28, tzinfo=UTC)
H = timedelta(hours=1)


def k(i: int, high: str, low: str, start: datetime = T0) -> Kline:
    t = start + H * i
    mid = (D(high) + D(low)) / 2
    return Kline(
        open_time=t, open=mid, high=D(high), low=D(low), close=mid,
        volume=D(1000), close_time=t + H,
    )


def row(direction: SignalDirection = SignalDirection.LONG, **kw: Any) -> so.Row:
    """LONG: зона 100–101, вход 101, стоп 99 (риск 2), тейк 106 (RR 2.5).
    SHORT: зона 99–100, вход 99, стоп 101, тейк 94."""
    long_ = direction is SignalDirection.LONG
    fields: dict[str, Any] = {
        "id": 1, "symbol": "BTC-USDT", "timeframe": "1h", "setup": BreakoutRetest.name,
        "direction": direction,
        "entry_low": D(100) if long_ else D(99),
        "entry_high": D(101) if long_ else D(100),
        "stop_loss": D(99) if long_ else D(101),
        "take_profit": D(106) if long_ else D(94),
        "notified_at": T0,
    }
    fields.update(kw)
    return so.Row(**fields)


class TestSimulate:
    def test_take(self) -> None:
        out = so.simulate(row(), [k(0, "102", "100.5"), k(1, "106.5", "101")])
        assert (out.kind, out.bar, out.entry_touched) == (so.TAKE, 2, True)

    def test_stop(self) -> None:
        out = so.simulate(row(), [k(0, "102", "101.5"), k(1, "101", "98.9")])
        assert (out.kind, out.bar) == (so.STOP, 2)

    def test_both_in_one_bar_is_stop(self) -> None:
        """Порядок внутри свечи неизвестен — консервативно стоп."""
        out = so.simulate(row(), [k(0, "107", "98")])
        assert out.kind == so.STOP

    def test_open_within_horizon(self) -> None:
        candles = [k(i, "103", "101.5") for i in range(10)]
        out = so.simulate(row(), candles, horizon=5)
        assert (out.kind, out.bar, out.bars_seen, out.entry_touched) == (so.OPEN, None, 5, False)

    def test_horizon_cuts_later_take(self) -> None:
        candles = [k(i, "103", "101.5") for i in range(5)] + [k(5, "107", "102")]
        assert so.simulate(row(), candles, horizon=5).kind == so.OPEN
        assert so.simulate(row(), candles, horizon=6).kind == so.TAKE

    def test_short(self) -> None:
        short = row(SignalDirection.SHORT)
        assert so.simulate(short, [k(0, "99.5", "93.9")]).kind == so.TAKE
        assert so.simulate(short, [k(0, "101", "98")]).kind == so.STOP


class TestScore:
    RATE = D("0.0005")

    def test_take_is_snapshot_rr_not_plus_two(self) -> None:
        r = so.score(row(), so.Outcome(so.TAKE, 1, 1, True), self.RATE)
        assert r.r_gross == D("2.5")
        # комиссия_R = rate × (вход + выход) / |вход − стоп| = 0.0005 × 207 / 2
        assert r.r_net == D("2.5") - self.RATE * D(207) / D(2)

    def test_stop_is_minus_one_minus_fee(self) -> None:
        r = so.score(row(), so.Outcome(so.STOP, 1, 1, True), self.RATE)
        assert r.r_gross == D(-1)
        assert r.r_net == D(-1) - self.RATE * D(200) / D(2)

    def test_short_uses_lower_edge(self) -> None:
        short = row(SignalDirection.SHORT)
        assert short.entry == D(99)
        r = so.score(short, so.Outcome(so.TAKE, 1, 1, True), self.RATE)
        assert r.r_gross == D(5) / D(2)

    def test_open_has_no_r(self) -> None:
        r = so.score(row(), so.Outcome(so.OPEN, None, 50, False), self.RATE)
        assert r.r_gross is None and r.r_net is None


class TestWindows:
    def test_forward_skips_candle_in_progress_at_notification_and_unclosed(self) -> None:
        notified = T0 + H * 2 + timedelta(minutes=30)
        candles = [k(i, "102", "101") for i in range(6)]
        now = T0 + H * 5 + timedelta(minutes=10)  # свеча 5 ещё не закрыта
        fwd = so.forward_window(candles, notified, now)
        assert [c.open_time for c in fwd] == [T0 + H * 3, T0 + H * 4]

    def test_history_is_closed_candles_scanner_window(self) -> None:
        candles = [k(i, "102", "101") for i in range(CANDLES_REQUIRED + 5)]
        notified = T0 + H * (CANDLES_REQUIRED + 2) + timedelta(minutes=5)
        hist = so.history_window(candles, notified)
        assert len(hist) == CANDLES_REQUIRED - 1
        assert hist[-1].close_time <= notified
        assert hist[-1].open_time == T0 + H * (CANDLES_REQUIRED + 1)


def _row_from_signal(chart: list[Kline], **kw: Any) -> so.Row:
    signal = BreakoutRetest().detect(build_context(chart))
    assert signal.is_actionable, signal.note
    fields: dict[str, Any] = {
        "id": 7, "symbol": "BTC-USDT", "timeframe": "4h", "setup": signal.setup,
        "direction": signal.direction,
        "entry_low": signal.entry_zone_low, "entry_high": signal.entry_zone_high,
        "stop_loss": signal.stop_loss, "take_profit": signal.take_profit_1,
        "notified_at": chart[-1].close_time,
    }
    fields.update(kw)
    return so.Row(**fields)


class TestReplay:
    def test_match_takes_features_from_detector(self) -> None:
        chart = breakout_retest_chart()
        r = _row_from_signal(chart)
        features, source = so.replay_features(r, chart)
        signal = BreakoutRetest().detect(build_context(chart))
        assert source == so.REPLAY
        assert features.breakout_volume_ratio == signal.breakout_volume_ratio
        assert features.breakout_at == chart[216].open_time
        assert features.atr == build_context(chart).atr
        # вход LONG — верхний край зоны 202.5, стоп 198.503
        assert features.stop_pct == (D("202.5000") - D("198.5030")) / D("202.5000") * 100

    def test_different_stop_is_mismatch(self) -> None:
        chart = breakout_retest_chart()
        r = _row_from_signal(chart, stop_loss=D("198"))
        _, source = so.replay_features(r, chart)
        assert source == so.REPLAY_MISMATCH

    def test_no_ready_on_replay_still_finds_breakout(self) -> None:
        """Прогон без READY (подтверждение на формирующейся свече) — пробой
        находится тем же методом детектора, признак помечен replay≠."""
        chart = breakout_retest_chart()
        r = _row_from_signal(chart)
        features, source = so.replay_features(r, chart[:-1])
        assert source == so.REPLAY_MISMATCH
        assert features.breakout_at == chart[216].open_time
        assert features.breakout_volume_ratio is not None

    def test_unknown_setup_is_mismatch_without_detector_features(self) -> None:
        r = replace(row(), setup="Что-то новое")
        features, source = so.replay_features(r, breakout_retest_chart())
        assert source == so.REPLAY_MISMATCH
        assert features.breakout_at is None
        assert features.stop_pct is not None


def _result(r: so.Row, kind: str = so.STOP) -> so.Result:
    return so.score(r, so.Outcome(kind, 1 if kind != so.OPEN else None, 1, True), D(0))


class TestSlices:
    def test_one_per_breakout_keeps_first(self) -> None:
        at = T0 - H * 5
        a = row(id=1, notified_at=T0, features=so.Features(breakout_at=at))
        b = row(id=2, notified_at=T0 + H, features=so.Features(breakout_at=at))
        c = row(id=3, notified_at=T0 + H, features=so.Features(breakout_at=at - H))
        d = row(id=4, features=so.Features())  # без breakout_at — вне среза
        e = row(id=5, setup=EMAPullback.name, features=so.Features(breakout_at=at))
        kept = so.one_per_breakout([_result(x) for x in (b, a, c, d, e)])
        assert [r.row.id for r in kept] == [1, 3]

    def test_open_outside_average(self) -> None:
        s = so.summarize("x", [_result(row(), so.TAKE), _result(row(), so.STOP),
                               _result(row(), so.OPEN)])
        assert (s.n, s.takes, s.stops, s.open) == (3, 1, 1, 1)
        assert s.avg_r_gross == (D("2.5") - 1) / 2
        assert s.sum_r_gross == D("1.5")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, "н/д"), (D("1.29"), "<1.3"), (D("1.3"), "1.3–1.5"), (D("1.5"), "≥1.5")],
    )
    def test_bucket(self, value: Decimal | None, expected: str) -> None:
        assert so.bucket(value, so.VOLUME_EDGES) == expected

    def test_render_has_all_slices_and_replay_share(self) -> None:
        a = row(id=1, features=so.Features(atr=D(1), volume_ratio_last=D(1)))
        b = replace(row(id=2), features_source=so.REPLAY)
        c = replace(row(id=3), features_source=so.REPLAY_MISMATCH)
        text = so.render([_result(a, so.TAKE), _result(b), _result(c, so.OPEN)], D(0), 50)
        assert "replay совпал со снимком 1/2 (50%)" in text
        for title in ("ТФ", "Сетап", "Направление", "Объём пробоя (пробой)",
                      "Стоп, % от входа", "Один сигнал на пробой"):
            assert f"== {title}\n" in text


class TestRowFromDb:
    BASE = (1, "BTC-USDT", "1h", BreakoutRetest.name, "LONG", D(100), D(101), D(99), D(106),
            T0, None, None, None, None, None, None)

    def test_parses_snapshot(self) -> None:
        r = so.row_from_db(self.BASE)
        assert r is not None and r.direction is SignalDirection.LONG and r.features.empty

    @pytest.mark.parametrize(("index", "value"), [(4, None), (4, "WAIT"), (7, None), (2, "15m")])
    def test_unusable_row_is_skipped_not_guessed(self, index: int, value: object) -> None:
        values = list(self.BASE)
        values[index] = value
        assert so.row_from_db(values) is None

    def test_render_reports_skipped(self) -> None:
        text = so.render([], D(0), 50, skipped=[5, 9])
        assert "Пропущено READY без направления/цен/известного ТФ: 2 (id 5, 9)" in text


class FakeClient:
    def __init__(self, remaining: int | None, candles: list[Kline]) -> None:
        self._rate_limits: dict[tuple[str, str], object] = {}
        self._remaining = remaining
        self._candles = candles
        self.calls: list[tuple[str, int, datetime]] = []

    async def get_klines(
        self, symbol: str, interval: str, limit: int, end_time: datetime
    ) -> list[Kline]:
        self.calls.append((interval, limit, end_time))
        if self._remaining is not None:
            self._rate_limits[("GET", QUOTE_KLINES)] = SimpleNamespace(remaining=self._remaining)
        return self._candles


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(so, "REQUEST_PAUSE_SECONDS", 0)


class TestFetcher:
    async def test_stops_at_remain_two(self) -> None:
        fetcher = so.KlineFetcher(FakeClient(2, []))
        with pytest.raises(so.RateLimitStopError):
            await fetcher.get("BTC-USDT", "1h", 10, T0)

    async def test_continues_above_two(self) -> None:
        fetcher = so.KlineFetcher(FakeClient(3, []))
        assert await fetcher.get("BTC-USDT", "1h", 10, T0) == []

    async def test_snapshot_row_needs_no_history_request(self) -> None:
        client = FakeClient(None, [k(i, "103", "101.5") for i in range(3)])
        snap = row(features=so.Features(atr=D(1), volume_ratio_last=D(1)))
        now = T0 + H * 10
        [result] = await so.evaluate([snap], so.KlineFetcher(client), D(0), 50, now)
        assert len(client.calls) == 1
        assert result.row.features_source == so.SNAPSHOT
        # stop_pct в снимке пуст — досчитан из цен снимка
        assert result.row.features.stop_pct == D(2) / D(101) * 100
        assert result.outcome.kind == so.OPEN
        # конец окна не в будущем
        assert client.calls[0][2] == now

    async def test_pre_migration_row_is_replayed(self) -> None:
        client = FakeClient(None, [])
        old = row()
        [result] = await so.evaluate([old], so.KlineFetcher(client), D(0), 50, T0 + H)
        assert len(client.calls) == 2
        assert client.calls[0][1] == CANDLES_REQUIRED
        assert result.row.features_source == so.REPLAY_MISMATCH


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")
async def test_load_rows_runs_read_only_select() -> None:
    """Запрос живой схемы проходит (колонки миграции ea93de72860d на месте)."""
    rows, skipped = await so.load_rows()
    assert all(isinstance(r, so.Row) for r in rows)
    assert all(isinstance(nid, int) for nid in skipped)
    assert so.SELECT_READY.lstrip().upper().startswith("SELECT")
