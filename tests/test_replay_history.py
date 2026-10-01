"""scripts/replay_history.py — чистая логика: слоты сканера, загрузка страницами,
funding, режим D1, калибровка, сводки. Сеть и БД не участвуют: свечи строятся
вручную, клиент биржи — фейк."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.analysis.engine import context_from_candles
from app.analysis.signals import Signal, wait_signal
from app.exchanges.base import Kline
from app.trading.enums import SignalDirection, SignalLevel
from scripts import replay_history as rh
from scripts.signal_outcomes import Features, Outcome, RateLimitStopError, Result, Row
from tests.test_setups import breakout_retest_chart

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
TTL = timedelta(hours=4)


def kline(i: int, close: str = "100", tf: timedelta = H, start: datetime = T0) -> Kline:
    t = start + tf * i
    c = D(close)
    return Kline(
        open_time=t, open=c, high=c + 1, low=c - 1, close=c, volume=D(1000), close_time=t + tf
    )


def row(**kw: Any) -> Row:
    base = Row(
        id=1, symbol="BTC-USDT", timeframe="1h", setup="Пробой с ретестом",
        direction=SignalDirection.LONG, entry_low=D(100), entry_high=D(101),
        stop_loss=D(99), take_profit=D(106), notified_at=T0,
    )
    return replace(base, **kw)


# --- слоты сканера -------------------------------------------------------------


class TestSlotStep:
    def test_first_ready_notifies(self) -> None:
        state = rh.SlotState()
        assert rh.slot_step(state, SignalLevel.READY, "a", T0, TTL) is True

    def test_same_fingerprint_within_ttl_is_quiet(self) -> None:
        state = rh.SlotState()
        rh.slot_step(state, SignalLevel.READY, "a", T0, TTL)
        assert rh.slot_step(state, SignalLevel.READY, "a", T0 + H, TTL) is False

    def test_same_fingerprint_after_ttl_notifies_again(self) -> None:
        state = rh.SlotState()
        rh.slot_step(state, SignalLevel.READY, "a", T0, TTL)
        assert rh.slot_step(state, SignalLevel.READY, "a", T0 + TTL, TTL) is True

    def test_changed_fingerprint_notifies(self) -> None:
        state = rh.SlotState()
        rh.slot_step(state, SignalLevel.READY, "a", T0, TTL)
        assert rh.slot_step(state, SignalLevel.READY, "b", T0 + H, TTL) is True

    @pytest.mark.parametrize("level", [None, SignalLevel.FORMING])
    def test_setup_gone_then_back_notifies(self, level: SignalLevel | None) -> None:
        """FORMING и «нет сетапа» гасят READY-слот — тот же сетап снова уведомляет."""
        state = rh.SlotState()
        rh.slot_step(state, SignalLevel.READY, "a", T0, TTL)
        assert rh.slot_step(state, level, None, T0 + H, TTL) is False
        assert rh.slot_step(state, SignalLevel.READY, "a", T0 + 2 * H, TTL) is True


# --- replay -------------------------------------------------------------------


def _ready(stop: str) -> Signal:
    return Signal(
        symbol="BTC-USDT", timeframe="1h", setup="Пробой с ретестом",
        direction=SignalDirection.LONG, entry_zone_low=D(100), entry_zone_high=D(101),
        stop_loss=D(stop), take_profit_1=D(106), confidence=70,
    )


def test_replay_series_follows_slots_and_window() -> None:
    """Скан на закрытии каждой свечи окна; уведомления — по правилам слотов;
    свечи до start (прогрев) не сканируются."""
    candles = [kline(i) for i in range(80)]
    start = candles[60].close_time
    plan = {62: _ready("99"), 63: _ready("99"), 64: _ready("98"), 70: _ready("98")}
    seen: list[datetime] = []

    def fake(context: Any) -> Signal:
        idx = len([c for c in candles if c.close_time <= context.candles[-1].close_time]) - 1
        seen.append(context.candles[-1].close_time)
        sig = plan.get(idx)
        return sig if sig is not None else wait_signal("BTC-USDT", "1h", "нет")

    rows = rh.replay_series(
        "BTC-USDT", "1h", candles, start, candles[-1].close_time, ttl=TTL, evaluate=fake
    )

    assert seen[0] == start
    assert [r.notified_at for r in rows] == [
        candles[62].close_time, candles[64].close_time, candles[70].close_time
    ]
    assert [r.stop_loss for r in rows] == [D(99), D(98), D(98)]
    assert rows[0].features_source == "replay"
    assert rows[0].features.stop_pct is not None


def test_replay_series_uses_at_most_candles_required_window() -> None:
    candles = [kline(i) for i in range(400)]
    sizes: list[int] = []

    def fake(context: Any) -> Signal:
        sizes.append(len(context.candles))
        return wait_signal("BTC-USDT", "1h", "нет")

    rh.replay_series(
        "BTC-USDT", "1h", candles, candles[350].close_time, candles[-1].close_time,
        evaluate=fake,
    )
    assert sizes and max(sizes) == rh.CANDLES_REQUIRED


def test_replay_with_real_engine_matches_engine_evaluate() -> None:
    """Настоящий движок на графике полного пробоя с ретестом: replay даёт
    уведомление на последней свече с теми же уровнями, что evaluate."""
    chart = breakout_retest_chart()
    expected = rh.engine_evaluate(context_from_candles("BTC-USDT", "1h", chart))
    rows = rh.replay_series("BTC-USDT", "1h", chart, chart[0].close_time, chart[-1].close_time)

    assert rows[-1].notified_at == chart[-1].close_time
    assert (rows[-1].stop_loss, rows[-1].take_profit) == (
        expected.stop_loss, expected.take_profit_1
    )
    assert rows[-1].setup == expected.setup


# --- загрузка ------------------------------------------------------------------


class FakeKlineFetcher:
    """Отдаёт до limit свечей, открытых не позже end_time, как биржа."""

    def __init__(self, candles: list[Kline]) -> None:
        self.candles = candles
        self.calls: list[datetime] = []

    async def get(self, symbol: str, tf: str, limit: int, end_time: datetime) -> list[Kline]:
        self.calls.append(end_time)
        older = [c for c in self.candles if c.open_time <= end_time]
        return older[-limit:]


async def test_fetch_klines_pages_back_to_start(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rh, "KLINE_LIMIT", 10)
    candles = [kline(i) for i in range(35)]
    fetcher = FakeKlineFetcher(candles)
    now = candles[-1].close_time

    got = await rh.fetch_klines(fetcher, "BTC-USDT", "1h", candles[7].open_time, now)

    assert [c.open_time for c in got] == [c.open_time for c in candles[7:]]
    assert len(fetcher.calls) == 3


async def test_fetch_klines_stops_when_history_ends(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Биржа не отдаёт старше — останавливаемся, а не крутимся."""
    monkeypatch.setattr(rh, "KLINE_LIMIT", 10)
    candles = [kline(i) for i in range(15)]
    fetcher = FakeKlineFetcher(candles)

    got = await rh.fetch_klines(
        fetcher, "BTC-USDT", "1h", T0 - 100 * H, candles[-1].close_time
    )

    assert len(got) == 15
    assert len(fetcher.calls) == 3


async def test_fetch_klines_keeps_only_closed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    candles = [kline(i) for i in range(5)]
    got = await rh.fetch_klines(
        FakeKlineFetcher(candles), "BTC-USDT", "1h", T0, candles[3].close_time
    )
    assert len(got) == 4


def test_klines_cache_roundtrip(tmp_path: Path) -> None:
    candles = [kline(i, close="100.12345") for i in range(3)]
    path = tmp_path / "x.jsonl.gz"
    rh.save_klines(path, candles)
    assert rh.load_klines(path, "1h") == candles


# --- funding ---------------------------------------------------------------------


def test_parse_funding_live_shape() -> None:
    """Форма живого ответа 01.10 (BTC-USDT, одна запись)."""
    events = rh.parse_funding([{
        "fundingRate": "0.00010000", "fundingTime": 1790841600000,
        "markPrice": "83384.8", "symbol": "BTC-USDT",
    }])
    assert events == [rh.FundingEvent(
        datetime.fromtimestamp(1790841600, UTC), D("0.00010000"), D("83384.8")
    )]


@pytest.mark.parametrize("missing", ["fundingRate", "fundingTime", "markPrice"])
def test_parse_funding_missing_field_is_error(missing: str) -> None:
    item = {"fundingRate": "0.0001", "fundingTime": 1790841600000, "markPrice": "1"}
    del item[missing]
    with pytest.raises(ValueError, match="битая запись"):
        rh.parse_funding([item])


def test_parse_funding_not_list_is_error() -> None:
    with pytest.raises(ValueError, match="ожидался список"):
        rh.parse_funding({"data": []})


class FakeFundingClient:
    def __init__(self, events: list[dict[str, Any]], remaining: int = 400) -> None:
        self.events = events
        self.params: list[dict[str, Any]] = []
        self._rate_limits = {("GET", rh.FUNDING_PATH): SimpleNamespace(remaining=remaining)}

    async def _request(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.params.append(params)
        end = params.get("endTime")
        older = [e for e in self.events if end is None or e["fundingTime"] <= end]
        return older[-params["limit"]:]


def _funding_items(n: int) -> list[dict[str, Any]]:
    return [
        {"fundingRate": "0.0001", "fundingTime": int((T0 + 8 * H * i).timestamp() * 1000),
         "markPrice": "100", "symbol": "BTC-USDT"}
        for i in range(n)
    ]


async def test_fetch_funding_pages_by_end_time(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rh, "FUNDING_LIMIT", 4)
    client = FakeFundingClient(_funding_items(10))
    fetcher = rh.FundingFetcher(client, pause=0)

    events = await rh.fetch_funding(fetcher, "BTC-USDT", T0, T0 + 8 * H * 9)

    assert len(events) == 10
    assert events[0].time == T0
    # первая страница — без endTime (ручка отвечает null на endTime в текущем периоде)
    assert "endTime" not in client.params[0]
    assert all("endTime" in p for p in client.params[1:])


async def test_fetch_funding_stops_when_end_time_ignored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Ручка не сдвигает выдачу по endTime — останавливаемся; покрытие с
    самого раннего полученного, раньше — «н/д» в funding_r."""
    monkeypatch.setattr(rh, "FUNDING_LIMIT", 4)

    class Stuck(FakeFundingClient):
        async def _request(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            self.params.append(params)
            return self.events[-4:]

    client = Stuck(_funding_items(10))
    events = await rh.fetch_funding(
        rh.FundingFetcher(client, pause=0), "BTC-USDT", T0, T0 + 100 * H
    )

    assert len(events) == 4
    assert len(client.params) == 2


async def test_funding_fetcher_stops_on_low_rate_limit() -> None:
    client = FakeFundingClient(_funding_items(3), remaining=2)
    with pytest.raises(RateLimitStopError):
        await rh.FundingFetcher(client, pause=0).get("BTC-USDT", T0 + 100 * H)


class TestFundingR:
    events = [
        rh.FundingEvent(T0 + 8 * H * i, D("0.0001"), D("100")) for i in range(4)
    ]

    def test_long_pays_positive_rate(self) -> None:
        """Удержание (T0; T0+16h]: два события × 0.0001 × 100 = 0.02 на единицу,
        риск 2 (вход 101, стоп 99) → −0.01R."""
        assert rh.funding_r(row(), T0, T0 + 16 * H, self.events) == D("-0.01")

    def test_short_receives_positive_rate(self) -> None:
        short = row(direction=SignalDirection.SHORT, stop_loss=D(102))
        # вход SHORT — нижний край зоны (100), риск 2
        assert rh.funding_r(short, T0, T0 + 16 * H, self.events) == D("0.01")

    def test_no_history_for_start_is_none(self) -> None:
        assert rh.funding_r(row(), T0 - H, T0 + 16 * H, self.events) is None
        assert rh.funding_r(row(), T0, T0 + H, []) is None


# --- режим D1 -----------------------------------------------------------------------


class TestD1Regime:
    days = timedelta(days=1)

    def _d1(self, n: int, close: str = "100") -> list[Kline]:
        return [kline(i, close, tf=self.days) for i in range(n)]

    def test_above_and_below(self) -> None:
        d1 = self._d1(210)
        at = d1[-1].close_time
        assert rh.d1_regime(d1, at, D(150)) == "выше EMA200 D1"
        assert rh.d1_regime(d1, at, D(50)) == "ниже EMA200 D1"

    def test_not_enough_closed_days(self) -> None:
        d1 = self._d1(210)
        assert rh.d1_regime(d1, d1[150].close_time, D(150)) == "н/д"

    def test_future_days_not_used(self) -> None:
        """EMA только по дням, закрытым к моменту сигнала."""
        d1 = self._d1(200) + self._d1(1, "100000")
        d1[-1] = kline(200, "100000", tf=self.days)
        assert rh.d1_regime(d1, d1[199].close_time, D(150)) == "выше EMA200 D1"


# --- исход и сводки ------------------------------------------------------------------


def _scored(kind: str, r_gross: str | None, r_net: str | None, *,
            r_nf: str | None = None, at: datetime = T0, **kw: Any) -> rh.Scored:
    res = Result(
        row(notified_at=at, **kw),
        Outcome(kind, 1 if kind != "открыт" else None, 1, True),
        None if r_gross is None else D(r_gross),
        None if r_net is None else D(r_net),
    )
    return rh.Scored(res, None, None if r_nf is None else D(r_nf), "выше EMA200 D1")


def test_line_averages_closed_and_counts_missing_funding() -> None:
    items = [
        _scored("тейк", "2.5", "2.4", r_nf="2.3"),
        _scored("стоп", "-1", "-1.1"),
        _scored("открыт", None, None),
    ]
    ln = rh.line("все", items)
    assert (ln.n, ln.takes, ln.stops, ln.open) == (3, 1, 1, 1)
    assert ln.r_gross == D("0.75")
    assert ln.r_net == D("0.65")
    assert ln.r_net_funding == D("2.3")
    assert ln.funding_missing == 1


def test_split_halves_by_midpoint() -> None:
    start, end = T0, T0 + 10 * H
    items = [_scored("стоп", "-1", "-1.1", at=T0 + i * H) for i in range(10)]
    first, second = rh.split_halves(items, start, end)
    assert len(first) == 5 and len(second) == 5
    assert max(s.result.row.notified_at for s in first) < min(
        s.result.row.notified_at for s in second
    )


def test_first_per_breakout_separates_repeats() -> None:
    br = T0 - 5 * H
    a = _scored("стоп", "-1", "-1.1", at=T0, features=Features(breakout_at=br))
    b = _scored("стоп", "-1", "-1.1", at=T0 + H, features=Features(breakout_at=br))
    c = _scored("тейк", "2", "1.9", at=T0 + 2 * H, features=Features(breakout_at=br - H))
    first, repeat = rh.first_per_breakout([b, a, c])
    assert first == [a, c] and repeat == [b]


def test_score_row_adds_funding_and_regime() -> None:
    """Стоп на первой свече после уведомления; funding за удержание."""
    r = row(notified_at=T0)
    candles = [kline(0, "100"), Kline(
        open_time=T0, open=D(100), high=D(100), low=D(98), close=D(98.5), volume=D(1),
        close_time=T0 + H,
    )]
    events = [rh.FundingEvent(T0 - H, D("0.0001"), D("100")),
              rh.FundingEvent(T0 + H, D("0.0001"), D("100"))]
    s = rh.score_row(r, candles[1:], events, [], now=T0 + 10 * H)
    assert s.result.outcome.kind == "стоп"
    assert s.r_funding == D("-0.005")
    assert s.r_net_funding == s.result.r_net + D("-0.005")
    assert s.regime == "н/д"


# --- калибровка -------------------------------------------------------------------------


class TestMatchLive:
    def test_exact_levels_within_window(self) -> None:
        live = [row(notified_at=T0 + 30 * timedelta(minutes=1), stop_loss=D("99.000000"))]
        assert rh.match_live(live, [row(notified_at=T0 + H)])[0][1] is not None
        assert rh.match_live(live, [row(notified_at=T0 - 2 * H)])[0][1] is not None

    def test_outside_window_or_other_levels_not_matched(self) -> None:
        live = [row(notified_at=T0)]
        assert rh.match_live(live, [row(notified_at=T0 + 2 * H)])[0][1] is None
        assert rh.match_live(live, [row(notified_at=T0 - 4 * H)])[0][1] is None
        assert rh.match_live(live, [row(stop_loss=D("98.9"))])[0][1] is None
        assert rh.match_live(live, [row(direction=SignalDirection.SHORT)])[0][1] is None
        assert rh.match_live(live, [row(timeframe="4h")])[0][1] is None

    def test_threshold(self) -> None:
        assert rh.calibration_passed(8, 10) is True
        assert rh.calibration_passed(7, 10) is False
        assert rh.calibration_passed(0, 0) is False


def test_load_live_reads_signal_outcomes_json(tmp_path: Path) -> None:
    path = tmp_path / "so.json"
    path.write_text(
        '[{"id": 5, "symbol": "SOL-USDT", "timeframe": "1h", "setup": "Пробой с ретестом",'
        ' "direction": "LONG", "entry_low": "10.000000000000", "entry_high": "10.5",'
        ' "stop_loss": "9.8", "take_profit": "11", "notified_at": "2026-09-08T06:15:00+00:00",'
        ' "outcome": "стоп"}]',
        encoding="utf-8",
    )
    [lv] = rh.load_live(path)
    assert lv.id == 5 and lv.direction is SignalDirection.LONG
    assert lv.entry_low == D(10)
    assert lv.notified_at == datetime(2026, 9, 8, 6, 15, tzinfo=UTC)


def test_history_gap_detects_short_history() -> None:
    start = T0 + 400 * H
    full = {"1h": [kline(i) for i in range(500)]}
    assert rh.history_gap("BTC-USDT", full, start) is None
    short = {"1h": [kline(i) for i in range(200, 500)]}
    assert "история с" in str(rh.history_gap("GRAMTON-USDT", short, start))
    assert "нет свечей" in str(rh.history_gap("X", {"1h": []}, start))


# --- критерий половин -------------------------------------------------------------------


def _half(long_net: str, short_net: str, n: int = 60) -> list[rh.Scored]:
    return [
        *(_scored("тейк", long_net, long_net, at=T0 + i * H) for i in range(n)),
        *(
            _scored("стоп", short_net, short_net, at=T0 + i * H,
                    direction=SignalDirection.SHORT, stop_loss=D(102))
            for i in range(n)
        ),
    ]


def _check(checks: list[rh.FilterCheck], name: str) -> rh.FilterCheck:
    return next(c for c in checks if c.name == name)


def test_filter_chosen_on_fit_and_holds_on_test() -> None:
    checks = rh.evaluate_filters(_half("0.5", "-1"), _half("0.3", "-1"), ["BTC-USDT"])
    long_only = _check(checks, "только LONG")
    assert long_only.chosen and long_only.eligible
    assert long_only.test_r == D("0.3")
    assert long_only.uplift == D("0.65")
    assert long_only.holds
    assert sum(c.chosen for c in checks) == 1
    # все кандидаты в отчёте, не только выбранный
    assert {"только SHORT", "без BTC-USDT", "по тренду D1 (LONG выше, SHORT ниже)"} <= {
        c.name for c in checks
    }


def test_filter_that_breaks_on_test_does_not_hold() -> None:
    checks = rh.evaluate_filters(_half("0.5", "-1"), _half("-0.9", "-1"), ["BTC-USDT"])
    long_only = _check(checks, "только LONG")
    assert long_only.chosen
    assert not long_only.holds


def test_filter_below_min_kept_not_eligible() -> None:
    checks = rh.evaluate_filters(_half("0.5", "-1", n=40), _half("0.3", "-1", n=40), [])
    assert not _check(checks, "только LONG").eligible
    assert not any(c.chosen and c.name == "только LONG" for c in checks)


def test_notification_counts_per_symbol_and_half() -> None:
    items = [_scored("стоп", "-1", "-1", at=T0 + i * H) for i in range(10)]
    text = rh.notification_counts(items, (T0, T0 + 5 * H, T0 + 10 * H))
    assert "| BTC-USDT | 5 |" in text and "| все | 5 |" in text


def test_funding_coverage_share() -> None:
    items = [
        _scored("тейк", "2", "1.9", r_nf="1.8"),
        _scored("стоп", "-1", "-1.1"),
        _scored("открыт", None, None),
    ]
    assert rh.funding_coverage(items) == "закрытых 2, без funding 1 (50%)"


async def test_funding_null_data_is_retried_then_list() -> None:
    """Ручка изредка отдаёт data: null — повтор, а не «ставок нет»."""

    class Flaky(FakeFundingClient):
        def __init__(self, events: list[dict[str, Any]], nulls: int) -> None:
            super().__init__(events)
            self.nulls = nulls

        async def _request(self, path: str, params: dict[str, Any]) -> Any:
            if self.nulls:
                self.nulls -= 1
                return None
            return await super()._request(path, params)

    fetcher = rh.FundingFetcher(Flaky(_funding_items(3), nulls=2), pause=0, null_pause=0)
    events = await fetcher.get("BTC-USDT", T0 + 100 * H)
    assert len(events) == 3
    assert fetcher.null_retries == 2


async def test_funding_null_data_every_time_is_error() -> None:
    class AlwaysNull(FakeFundingClient):
        async def _request(self, path: str, params: dict[str, Any]) -> Any:
            self.params.append(params)
            return None

    client = AlwaysNull([])
    with pytest.raises(ValueError, match="NoneType"):
        await rh.FundingFetcher(client, pause=0, null_pause=0).get("BTC-USDT", T0)
    assert len(client.params) == rh.NULL_RETRIES + 1


# --- ложный пробой по тренду (--strategy fbo) ---------------------------------------------


def test_d1_ema_lookup_uses_only_closed_days() -> None:
    days = timedelta(days=1)
    d1 = [kline(i, "100", tf=days) for i in range(205)]
    lookup = rh.d1_ema_lookup(d1)
    at = d1[202].close_time
    before = lookup(at)
    assert before is not None
    future = [*d1[:203], kline(203, "100000", tf=days), kline(204, "100000", tf=days)]
    assert rh.d1_ema_lookup(future)(at) == before
    assert lookup(d1[100].close_time) is None   # меньше 200 закрытых дней
    assert lookup(T0) is None


class TestFundingAllows:
    events = [
        rh.FundingEvent(T0, D("0.0001"), D("1")),
        rh.FundingEvent(T0 + 8 * H, D("-0.0002"), D("1")),
        rh.FundingEvent(T0 + 16 * H, D("0"), D("1")),
    ]

    def test_sign_by_direction(self) -> None:
        short = row(direction=SignalDirection.SHORT, stop_loss=D(102), notified_at=T0 + H)
        long_ = row(notified_at=T0 + 9 * H)
        assert rh.funding_allows(short, self.events)          # ставка +0.0001
        assert not rh.funding_allows(row(notified_at=T0 + H), self.events)
        assert rh.funding_allows(long_, self.events)          # ставка −0.0002
        assert not rh.funding_allows(
            row(direction=SignalDirection.SHORT, stop_loss=D(102), notified_at=T0 + 9 * H),
            self.events,
        )

    def test_zero_or_no_rate_rejected(self) -> None:
        assert not rh.funding_allows(row(notified_at=T0 + 17 * H), self.events)
        assert not rh.funding_allows(row(notified_at=T0 - H), self.events)

    def test_future_rate_not_used(self) -> None:
        """Ставка начисления после уведомления не смотрится."""
        long_ = row(notified_at=T0 + 7 * H)  # последняя известная +0.0001
        assert not rh.funding_allows(long_, self.events)


class TestBootstrap:
    def test_deterministic_and_contains_mean(self) -> None:
        values = [D(x) for x in ("-1", "-1", "2.5", "-1", "1.8", "-1", "3", "-1.1")]
        a = rh.bootstrap_ci(values)
        assert a is not None and a == rh.bootstrap_ci(values)
        mean = sum(values, D(0)) / len(values)
        assert a[0] <= mean <= a[1]

    def test_constant_and_short(self) -> None:
        assert rh.bootstrap_ci([D("0.5")] * 10) == (D("0.5000"), D("0.5000"))
        assert rh.bootstrap_ci([D(1)]) is None


def _hs(closed: int, r: str | None, ci: tuple[str, str] | None) -> rh.HalfStats:
    return rh.HalfStats(
        n=closed, closed=closed, per_day=D(1), win_pct=None, r_gross=None, r_net=None,
        r_nf=None if r is None else D(r), ci=None if ci is None else (D(ci[0]), D(ci[1])),
        short=(0, 0, None), long=(0, 0, None),
    )


def test_half_ok_thresholds() -> None:
    assert rh.half_ok(_hs(30, "0.2", ("-0.05", "0.4")))
    assert not rh.half_ok(_hs(29, "0.2", ("-0.05", "0.4")))       # < 30 закрытых
    assert not rh.half_ok(_hs(40, "0", ("-0.05", "0.1")))         # R не > 0
    assert not rh.half_ok(_hs(40, "0.2", ("-0.1", "0.4")))        # граница ДИ не > −0.1
    assert not rh.half_ok(_hs(40, "0.2", None))


def test_fbo_select_both_ways_excludes_old() -> None:
    stats = {
        rh.OLD_STRATEGY: (_hs(100, "0.9", ("0.5", "1")), _hs(100, "0.9", ("0.5", "1"))),
        "A": (_hs(40, "0.3", ("0", "0.6")), _hs(40, "-0.2", ("-0.5", "0.1"))),
        "B": (_hs(20, "0.8", ("0.1", "1")), _hs(50, "0.4", ("0.1", "0.7"))),
    }
    name, fit, test = rh.fbo_select(stats, 0)  # type: ignore[misc]
    assert name == "A" and test.r_nf == D("-0.2")      # B исключена: 20 закрытых
    name, fit, test = rh.fbo_select(stats, 1)  # type: ignore[misc]
    assert name == "B" and test.closed == 20


def test_fbo_setup_sixteen_configs() -> None:
    evaluators, configs = rh.fbo_setup()
    fbo = [c for c in configs if c.name != rh.OLD_STRATEGY]
    assert len(fbo) == 16 and len({c.name for c in configs}) == 17
    assert len(evaluators) == 1 + 12                    # старая + 12 детекторов
    funding = [c for c in fbo if c.funding]
    assert len(funding) == 4
    assert all(not c.detector.endswith(("объём", "подтв")) for c in funding)
    assert all(c.detector in evaluators for c in configs)


def test_replay_multi_one_context_per_candle_and_separate_slots() -> None:
    candles = [kline(i) for i in range(70)]
    seen: list[int] = []
    d1_seen: list[Any] = []

    def first(context: Any) -> Signal:
        seen.append(id(context))
        d1_seen.append(context.d1_ema200)
        return _ready("99")

    def second(context: Any) -> Signal:
        seen.append(id(context))
        return _ready("99") if len(seen) % 4 == 0 else wait_signal("BTC-USDT", "1h", "нет")

    rows = rh.replay_multi(
        "BTC-USDT", "1h", candles, candles[60].close_time, candles[-1].close_time,
        {"a": first, "b": second}, ttl=timedelta(hours=100), d1_ema=lambda at: D(7),
    )
    assert seen[0::2] == seen[1::2]                      # один контекст на свечу
    assert set(d1_seen) == {D(7)}
    assert len(rows["a"]) == 1                           # слот «a» не гаснет — одно уведомление
    assert len(rows["b"]) > 1                            # у «b» свой слот, гаснет и снова READY
    ids = [r.id for rs in rows.values() for r in rs]
    assert len(ids) == len(set(ids))


def test_parse_end() -> None:
    assert rh.parse_end("2026-10-01T16:13Z") == datetime(2026, 10, 1, 16, 13, tzinfo=UTC)
    with pytest.raises(Exception, match="нужна зона"):
        rh.parse_end("2026-10-01T16:13")


# --- финальный тест: одна конфигурация, покрытие funding, режимы -------------------------


def test_select_fbo_only_named_plus_old() -> None:
    evaluators, configs = rh.select_fbo(["d=0.3 N=1 · funding"])
    assert [c.name for c in configs] == [rh.OLD_STRATEGY, "d=0.3 N=1 · funding"]
    assert set(evaluators) == {rh.OLD_STRATEGY, "d=0.3 N=1"}   # funding — строки базы
    with pytest.raises(ValueError, match="неизвестные"):
        rh.select_fbo(["d=0.2 N=1 · funding"])
    assert len(rh.select_fbo(None)[1]) == 17


def test_funding_gap() -> None:
    ev = [rh.FundingEvent(T0, D("0.0001"), D("1"))]
    assert rh.funding_gap("BTC-USDT", ev, T0) is None
    assert rh.funding_gap("BTC-USDT", ev, T0 + H) is None
    assert "funding с" in str(rh.funding_gap("BTC-USDT", ev, T0 - H))
    assert "funding нет" in str(rh.funding_gap("BTC-USDT", [], T0))


def test_regime_stats_and_render() -> None:
    items = [
        _scored("тейк", "2", "1.9", r_nf="1.9"),
        rh.Scored(
            Result(row(), Outcome("стоп", 1, 1, True), D(-1), D("-1.1")),
            None, D("-1.1"), "ниже EMA200 D1",
        ),
    ]
    stats = rh.regime_stats(items, 1.0)
    assert set(stats) == {"выше EMA200 D1", "ниже EMA200 D1"}
    assert stats["ниже EMA200 D1"].r_nf == D("-1.1")
    text = rh.render_regimes("4h", {"X": (stats, {})})
    assert "| X | выше EMA200 D1 | 1 · 1 · +1.90" in text and "| 0 |" in text


def test_render_fbo_single_config_has_no_selection() -> None:
    hs = _hs(40, "0.2", ("-0.05", "0.4"))
    text = rh.render_fbo(
        "4h", {rh.OLD_STRATEGY: (hs, hs), "d=0.3 N=1 · funding": (hs, hs)}, (T0, T0 + H, T0 + 2 * H)
    )
    assert "отбор в обе стороны" not in text
    assert "конфигураций 1 и старая" in text
    cell = "40 · +0.20 · [-0.05; +0.40]"
    assert f"| d=0.3 N=1 · funding | {cell} | {cell} | да |" in text


async def test_funding_null_on_older_page_is_end_of_history(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Старая страница отвечает null (раньше начала истории символа) — конец
    истории: возвращаем полученное, покрытие проверит funding_gap."""
    monkeypatch.setattr(rh, "FUNDING_LIMIT", 4)

    class Listed(FakeFundingClient):
        async def _request(self, path: str, params: dict[str, Any]) -> Any:
            self.params.append(params)
            if "endTime" in params:
                return None
            return self.events[-4:]

    client = Listed(_funding_items(10))
    fetcher = rh.FundingFetcher(client, pause=0, null_pause=0)
    events = await rh.fetch_funding(fetcher, "GRAMTON-USDT", T0, T0 + 100 * H)
    assert len(events) == 4
    assert rh.funding_gap("GRAMTON-USDT", events, T0) is not None
