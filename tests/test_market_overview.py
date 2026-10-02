"""Техническая картина «Анализа рынка» (app/analysis/market_overview.py):
факты по таймфреймам на ручных свечах, уровни, funding, OI, текст и снимок
AnalysisEngine.market_snapshot(). Без сети и БД."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.analysis import market_overview as mo
from app.analysis.engine import AnalysisEngine
from app.analysis.market_overview import (
    LevelFact,
    MarketOverview,
    TimeframeFacts,
    build_overview,
    frame_line,
    funding_text,
    nearest_levels,
    open_interest_text,
    render_overview,
    timeframe_facts,
    trend_label,
)
from app.exchanges.base import ExchangeUnavailableError, Kline, OpenInterest, PremiumIndex
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.trading.enums import MarketStructure

D = Decimal
NOW = datetime(2026, 10, 2, 6, 0, tzinfo=UTC)
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(i: int, o: Decimal, h: Decimal, low: Decimal, c: Decimal, volume: str = "100") -> Kline:
    t = BASE + timedelta(hours=i)
    return Kline(open_time=t, open=o, high=h, low=low, close=c, volume=D(volume),
                 close_time=t + timedelta(hours=1))


def _zigzag(n_pivots: int, *, up: bool, steps: int = 3) -> list[Kline]:
    """Пила с растущими (или падающими) экстремумами: HH/HL или LH/LL."""
    pivots: list[tuple[Decimal, str]] = []
    for k in range(n_pivots):
        base = D(100) + D(k // 2) * D(3) if up else D(400) - D(k // 2) * D(3)
        if k % 2 == 0:
            pivots.append((base, "low" if up else "high"))
        else:
            pivots.append((base + (D(8) if up else D(-8)), "high" if up else "low"))
    candles: list[Kline] = []
    prev = pivots[0][0]
    for target, kind in pivots:
        step = (target - prev) / steps
        for s in range(1, steps + 1):
            price = prev + step * s
            reached = s == steps
            high = target if reached and kind == "high" else price + D("0.5")
            low = target if reached and kind == "low" else price - D("0.5")
            candles.append(_bar(len(candles), price - step / 2, high, low, price))
        prev = target
    return candles


def _facts(**overrides: object) -> TimeframeFacts:
    fields: dict[str, object] = dict(
        timeframe="4h", close=D(100), ema50=D(95), ema200=D(90), rsi=D("61.4"),
        atr=D("2.1"), atr_percent=D("2.1"), volume_ratio=D("1.35"),
        structure=MarketStructure.UPTREND,
    )
    fields.update(overrides)
    return TimeframeFacts(**fields)  # type: ignore[arg-type]


def _premium(**overrides: object) -> PremiumIndex:
    fields: dict[str, object] = dict(
        symbol="BTC-USDT", mark_price=D("101.5"), index_price=D("101.4"),
        last_funding_rate=D("0.0001"), next_funding_time=NOW + timedelta(hours=3, minutes=12),
        funding_interval_hours=8,
    )
    fields.update(overrides)
    return PremiumIndex(**fields)  # type: ignore[arg-type]


def _oi(value: str) -> OpenInterest:
    return OpenInterest(symbol="BTC-USDT", value_usdt=D(value), time=NOW)


# --- факты таймфрейма --------------------------------------------------------


class TestTimeframeFacts:
    def test_uptrend_zigzag(self) -> None:
        candles = _zigzag(140, up=True)
        facts = timeframe_facts("4h", candles)
        assert facts is not None
        assert facts.structure is MarketStructure.UPTREND
        assert facts.close > facts.ema50 > facts.ema200  # type: ignore[operator]
        assert trend_label(facts) == "↑ выше EMA50/200"
        assert facts.atr_percent == facts.atr / facts.close * 100  # type: ignore[operator]

    def test_downtrend_zigzag(self) -> None:
        facts = timeframe_facts("1d", _zigzag(140, up=False))
        assert facts is not None
        assert facts.structure is MarketStructure.DOWNTREND
        assert trend_label(facts) == "↓ ниже EMA50/200"

    def test_short_history_is_none(self) -> None:
        assert timeframe_facts("1h", _zigzag(10, up=True)) is None

    def test_no_ema200_below_200_candles(self) -> None:
        facts = timeframe_facts("1d", _zigzag(40, up=True))  # 120 свечей
        assert facts is not None and facts.ema200 is None and facts.ema50 is not None
        assert trend_label(facts) == "выше EMA50, EMA200 н/д"


class TestTrendLabel:
    @pytest.mark.parametrize(
        ("ema50", "ema200", "label"),
        [
            ("95", "90", "↑ выше EMA50/200"),
            ("105", "110", "↓ ниже EMA50/200"),
            ("95", "110", "↗ выше EMA50, ниже EMA200"),
            ("105", "90", "↘ ниже EMA50, выше EMA200"),
        ],
    )
    def test_four_cases(self, ema50: str, ema200: str, label: str) -> None:
        assert trend_label(_facts(ema50=D(ema50), ema200=D(ema200))) == label

    def test_no_ema_at_all(self) -> None:
        assert trend_label(_facts(ema50=None, ema200=None)) == "EMA н/д"


class TestFrameLine:
    def test_all_parts(self) -> None:
        line = frame_line(_facts())
        assert line == "<b>H4</b> ↑ выше EMA50/200 · HH/HL · RSI 61 · ATR 2.1% · объём ×1.4"

    @pytest.mark.parametrize(
        ("structure", "label"),
        [(MarketStructure.DOWNTREND, "LH/LL"), (MarketStructure.RANGE, "диапазон"),
         (MarketStructure.UNDEFINED, "структура н/д")],
    )
    def test_structure_labels(self, structure: MarketStructure, label: str) -> None:
        assert f"· {label} ·" in frame_line(_facts(structure=structure))

    def test_tiny_atr_keeps_two_decimals(self) -> None:
        assert "ATR 0.05%" in frame_line(_facts(atr_percent=D("0.0512")))

    def test_missing_optional_numbers_are_skipped(self) -> None:
        line = frame_line(_facts(rsi=None, atr_percent=None, volume_ratio=None))
        assert "RSI" not in line and "ATR" not in line and "объём" not in line


# --- уровни --------------------------------------------------------------------


class TestNearestLevels:
    @staticmethod
    def _patch(monkeypatch, levels_by_tf: dict[str, list[Decimal]]) -> None:  # type: ignore[no-untyped-def]
        from app.analysis.structure import Level

        def fake_find(candles, atr_value):  # type: ignore[no-untyped-def]
            tf = candles[0].volume  # метка таймфрейма спрятана в объёме
            return [
                Level(price=p, touches=2, last_touch_index=0, is_resistance=True,
                      strength=D("0.5"))
                for p in levels_by_tf[str(tf)]
            ]

        monkeypatch.setattr(mo, "find_levels", fake_find)

    @staticmethod
    def _candles(tag: str) -> list[Kline]:
        return [_bar(i, D(100), D(101), D(99), D(100), volume=tag) for i in range(60)]

    def test_roles_by_price_two_per_side_nearest_first(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._patch(monkeypatch, {
            "4": [D(104), D(110), D(96)], "1": [D(103), D(90), D(95)],
        })
        res, sup = nearest_levels({"4h": self._candles("4"), "1d": self._candles("1")},
                                  D(100), D(0))
        assert [(lv.price, lv.timeframe) for lv in res] == [(D(103), "1d"), (D(104), "4h")]
        assert [(lv.price, lv.timeframe) for lv in sup] == [(D(96), "4h"), (D(95), "1d")]

    def test_close_levels_are_merged(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._patch(monkeypatch, {"4": [D("103.0"), D(110)], "1": [D("103.2")]})
        res, _ = nearest_levels({"4h": self._candles("4"), "1d": self._candles("1")},
                                D(100), D("0.5"))
        assert [lv.price for lv in res] == [D("103.0"), D(110)]

    def test_level_at_price_is_support(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._patch(monkeypatch, {"4": [D(100)], "1": []})
        res, sup = nearest_levels({"4h": self._candles("4"), "1d": self._candles("1")},
                                  D(100), D(0))
        assert res == () and [lv.price for lv in sup] == [D(100)]

    def test_h1_levels_are_not_used(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._patch(monkeypatch, {"7": [D(101)], "4": [], "1": []})
        res, sup = nearest_levels({"1h": self._candles("7"), "4h": self._candles("4"),
                                   "1d": self._candles("1")}, D(100), D(0))
        assert res == () and sup == ()


# --- funding и OI --------------------------------------------------------------


class TestFunding:
    def test_positive_with_countdown(self) -> None:
        assert funding_text(_premium(), NOW) == "+0.0100% · следующее через 3 ч 12 мин"

    def test_negative_minutes_only(self) -> None:
        text = funding_text(
            _premium(last_funding_rate=D("-0.000025"),
                     next_funding_time=NOW + timedelta(minutes=7, seconds=30)), NOW
        )
        assert text == "-0.0025% · следующее через 7 мин"

    def test_zero_rate_has_no_sign(self) -> None:
        assert funding_text(_premium(last_funding_rate=D(0)), NOW).startswith("0.0000%")

    def test_past_time(self) -> None:
        text = funding_text(_premium(next_funding_time=NOW - timedelta(seconds=1)), NOW)
        assert text.endswith("начисление сейчас")


class TestOpenInterestText:
    @pytest.mark.parametrize(
        ("value", "text"),
        [("908418144.5", "908.4 млн USDT"), ("1234567890", "1.23 млрд USDT"),
         ("47783401.5019", "47.8 млн USDT"), ("512340", "512.3 тыс. USDT")],
    )
    def test_scales(self, value: str, text: str) -> None:
        assert open_interest_text(_oi(value)) == text


# --- обзор и текст ---------------------------------------------------------------


def _overview(**overrides: object) -> MarketOverview:
    fields: dict[str, object] = dict(
        symbol="BTC-USDT", price=D("101.5"),
        frames=(_facts(timeframe="1h"), _facts(timeframe="4h"), _facts(timeframe="1d")),
        resistances=(LevelFact(D("103.5"), "4h", 3), LevelFact(D(110), "1d", 2)),
        supports=(LevelFact(D("98.25"), "4h", 2),),
        premium=_premium(), open_interest=_oi("908418144.5"),
    )
    fields.update(overrides)
    return MarketOverview(**fields)  # type: ignore[arg-type]


class TestRender:
    def test_layout(self) -> None:
        text = render_overview(_overview(), 2, NOW)
        lines = text.splitlines()
        assert lines[0] == "🔎 <b>BTC-USDT</b> · цена 101.5"
        assert [ln[:10] for ln in lines[2:5]] == ["<b>H1</b> ", "<b>H4</b> ", "<b>D1</b> "]
        assert "Сопротивление: 103.5 (H4) · 110 (D1)" in lines
        assert "Поддержка: 98.25 (H4)" in lines
        assert "Funding: +0.0100% · следующее через 3 ч 12 мин" in lines
        assert "Open interest: 908.4 млн USDT" in lines

    def test_no_trade_direction_or_advice(self) -> None:
        text = render_overview(_overview(), 2, NOW).lower()
        for word in ("long", "short", "лонг", "шорт", "вход", "покуп", "продаж", "wait", "ready"):
            assert word not in text, word

    def test_missing_premium_oi_and_levels(self) -> None:
        text = render_overview(
            _overview(premium=None, open_interest=None, resistances=(), supports=()), 2, NOW
        )
        assert "Funding: н/д" in text and "Open interest: н/д" in text
        assert "Сопротивление: нет в пределах истории" in text


class TestBuildOverview:
    def test_uses_mark_price_and_skips_short_frames(self) -> None:
        candles = {"1h": _zigzag(140, up=True), "4h": _zigzag(140, up=True),
                   "1d": _zigzag(5, up=True)}
        ov = build_overview("BTC-USDT", candles, _premium(), _oi("1"))
        assert ov is not None
        assert ov.price == D("101.5")
        assert [f.timeframe for f in ov.frames] == ["1h", "4h"]

    def test_without_premium_price_is_h1_close(self) -> None:
        candles = {"1h": _zigzag(140, up=True)}
        ov = build_overview("BTC-USDT", candles, None, None)
        assert ov is not None and ov.price == candles["1h"][-1].close

    def test_no_data_is_none(self) -> None:
        assert build_overview("BTC-USDT", {"1h": [], "4h": [], "1d": []}, None, None) is None


# --- снимок движка -------------------------------------------------------------


class _Client:
    name = "bingx"

    def __init__(self, *, premium_error: bool = False) -> None:
        self.kline_calls: list[tuple[str, int]] = []
        self.premium_calls = 0
        self.oi_calls = 0
        self._premium_error = premium_error

    async def get_klines(self, symbol, interval, limit=500, end_time=None):  # type: ignore[no-untyped-def]
        self.kline_calls.append((interval, limit))
        return _zigzag(140, up=True)

    async def get_premium_index(self, symbol: str) -> PremiumIndex:
        self.premium_calls += 1
        if self._premium_error:
            raise ExchangeUnavailableError("down")
        return _premium(symbol=symbol)

    async def get_open_interest(self, symbol: str) -> OpenInterest:
        self.oi_calls += 1
        return _oi("1000000")


class TestMarketSnapshot:
    async def test_five_requests_d1_depth_1000(self) -> None:
        client = _Client()
        engine = AnalysisEngine(MarketDataService(client, TTLCache()))  # type: ignore[arg-type]
        snap = await engine.market_snapshot("BTC-USDT")
        assert client.kline_calls == [("1h", 300), ("4h", 300), ("1d", 1000)]
        assert (client.premium_calls, client.oi_calls) == (1, 1)
        assert set(snap.candles) == {"1h", "4h", "1d"}
        assert snap.premium is not None and snap.open_interest is not None

    async def test_premium_failure_is_none_not_error(self) -> None:
        client = _Client(premium_error=True)
        engine = AnalysisEngine(MarketDataService(client, TTLCache()))  # type: ignore[arg-type]
        snap = await engine.market_snapshot("BTC-USDT")
        assert snap.premium is None and snap.open_interest is not None

    async def test_market_data_caches_quotes(self) -> None:
        client = _Client()
        market = MarketDataService(client, TTLCache())  # type: ignore[arg-type]
        await market.get_premium_index("BTC-USDT")
        await market.get_premium_index("BTC-USDT")
        await market.get_open_interest("BTC-USDT")
        await market.get_open_interest("BTC-USDT")
        assert (client.premium_calls, client.oi_calls) == (1, 1)


def test_frames_keep_timeframe_order() -> None:
    ov = _overview()
    assert [f.timeframe for f in replace(ov).frames] == list(mo.OVERVIEW_TIMEFRAMES)
