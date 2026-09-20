"""Тесты рендера графика сетапа (app/analysis/charting.py).

render_setup_chart не должен бросать исключения наружу ни при каких
входных данных — сканер шлёт текст уведомления независимо от того,
построилась картинка или нет (см. app/workers/scanner.py).
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg

from app.analysis import charting
from app.analysis.charting import _nearby_levels, render_analysis_chart, render_setup_chart
from app.analysis.signals import MarketContext, Signal, SignalCondition
from app.analysis.structure import Level
from app.exchanges.base import Kline
from app.trading.enums import MarketStructure, SignalDirection, SignalLevel

D = Decimal
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _candles(n: int = 220) -> list[Kline]:
    now = datetime.now(UTC)
    price = D("100")
    candles = []
    for i in range(n):
        price += D(i % 5 - 2) * D("0.3")
        open_time = now - timedelta(hours=n - i)
        candles.append(
            Kline(
                open_time=open_time,
                open=price,
                high=price + D("1"),
                low=price - D("1"),
                close=price + D("0.2"),
                volume=D("1000"),
                close_time=open_time + timedelta(hours=1),
            )
        )
    return candles


def _context(candles: list[Kline] | None = None) -> MarketContext:
    candles = _candles() if candles is None else candles
    return MarketContext(
        symbol="BTC-USDT",
        timeframe="1h",
        candles=candles,
        price=candles[-1].close if candles else D("1"),
        ema20=D("99"),
        ema50=D("98"),
        ema200=D("95"),
        rsi=D("55"),
        atr=D("1.5"),
        macd_histogram=D("0.1"),
        volume_ratio=D("1.2"),
        structure=MarketStructure.UPTREND,
        levels=[],
    )


def _signal(**overrides) -> Signal:
    defaults = dict(
        symbol="BTC-USDT",
        timeframe="1h",
        direction=SignalDirection.LONG,
        setup="Пробой с ретестом",
        entry_zone_low=D("99"),
        entry_zone_high=D("100"),
        stop_loss=D("97"),
        take_profit_1=D("106"),
        level_price=D("99.5"),
        confidence=7,
    )
    defaults.update(overrides)
    return Signal(**defaults)


class TestRenderSetupChart:
    def test_returns_valid_png(self) -> None:
        png = render_setup_chart(_context(), _signal(), SignalLevel.READY)
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_narrow_entry_zone_around_level_still_renders(self) -> None:
        # Ретест по конструкции происходит у самого уровня — зона входа и
        # уровень почти совпадают. Раньше подписи на графике накладывались
        # друг на друга в этом случае; теперь подписи вынесены в легенду.
        signal = _signal(
            entry_zone_low=D("99.4"), entry_zone_high=D("99.6"), level_price=D("99.5"),
        )
        png = render_setup_chart(_context(), signal, SignalLevel.FORMING)
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_missing_optional_fields_still_renders(self) -> None:
        # FORMING-сигнал от EMAPullback: нет level_price и зоны входа.
        signal = _signal(
            direction=SignalDirection.WAIT,
            entry_zone_low=None, entry_zone_high=None,
            stop_loss=None, take_profit_1=None, level_price=None,
        )
        png = render_setup_chart(_context(), signal, SignalLevel.FORMING)
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_broken_context_returns_none_not_raises(self) -> None:
        """Пустые свечи ломают расчёт EMA — рендер обязан проглотить это
        и вернуть None, а не уронить вызывающий код."""
        broken = _context(candles=[])
        result = render_setup_chart(broken, _signal(), SignalLevel.FORMING)
        assert result is None


def _level(price: Decimal, *, resistance: bool) -> Level:
    return Level(
        price=price, touches=2, last_touch_index=1, is_resistance=resistance, strength=D("0.5")
    )


def _windowed_context(*, wide_at: int = -50) -> MarketContext:
    """Свечи около 100 и одна широкая [90; 110]: окно видимых свечей — [90; 110],
    ATR 1.5 → окно уровней [88.5; 111.5]. Цена последней свечи ~100."""
    candles = _candles()
    candles[wide_at] = replace(candles[wide_at], high=D("110"), low=D("90"))
    context = _context(candles)
    assert D("98") < context.price < D("103")
    return context


class TestRenderAnalysisChart:
    """График экрана «Анализ рынка»: рисуется и при найденном сетапе, и при
    FORMING, и при WAIT — при WAIT видно, чего ждём."""

    def test_found_setup_renders(self) -> None:
        png = render_analysis_chart(_context(), _signal())
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_forming_renders(self) -> None:
        signal = Signal(
            symbol="BTC-USDT", timeframe="1h", direction=SignalDirection.WAIT,
            setup="Нет сетапа",
            conditions=[SignalCondition("Подтверждающий паттерн", False, "")],
        )
        png = render_analysis_chart(_context(), signal)
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_wait_with_nearby_levels_renders(self) -> None:
        context = replace(
            _windowed_context(),
            levels=[
                _level(D("105"), resistance=True),
                _level(D("108"), resistance=True),
                _level(D("95"), resistance=False),
            ],
        )
        signal = Signal(
            symbol="BTC-USDT", timeframe="1h", direction=SignalDirection.WAIT,
            setup="Нет сетапа", level_price=D("105"),
        )
        png = render_analysis_chart(context, signal)
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_nearby_levels_are_two_per_side_nearest_first(self) -> None:
        context = _windowed_context()
        levels = [
            _level(D(x), resistance=D(x) > context.price)
            for x in (108, 103, 105, 98, 95, 92)  # все внутри окна [88.5; 111.5]
        ]
        picked = _nearby_levels(replace(context, levels=levels))
        assert [lv.price for lv in picked] == [D(103), D(105), D(98), D(95)]

    def test_levels_outside_visible_window_are_dropped(self) -> None:
        context = _windowed_context()
        levels = [
            _level(D("130"), resistance=True),   # выше окна
            _level(D("105"), resistance=True),
            _level(D("95"), resistance=False),
            _level(D("60"), resistance=False),   # ниже окна: как поддержка 1892 у ETH 4H
        ]
        picked = _nearby_levels(replace(context, levels=levels))
        assert [lv.price for lv in picked] == [D("105"), D("95")]

    def test_window_edge_includes_one_atr_margin(self) -> None:
        context = _windowed_context()  # окно уровней [88.5; 111.5]
        levels = [
            _level(D("111.4"), resistance=True),
            _level(D("111.6"), resistance=True),
            _level(D("88.6"), resistance=False),
            _level(D("88.4"), resistance=False),
        ]
        picked = _nearby_levels(replace(context, levels=levels))
        assert [lv.price for lv in picked] == [D("111.4"), D("88.6")]

    def test_window_covers_only_displayed_candles(self) -> None:
        """Широкая свеча старше CANDLES_DISPLAYED окно не расширяет."""
        context = _windowed_context(wide_at=-150)
        shown = context.candles[-charting.CANDLES_DISPLAYED :]
        high = max(c.high for c in shown)
        assert high < D("105")  # старая свеча 110 в окно не входит
        inside = high  # верхняя граница окна свечей
        outside = high + context.atr + D("1")  # за пределами даже с запасом в 1 ATR
        levels = [_level(inside, resistance=True), _level(outside, resistance=True)]
        picked = _nearby_levels(replace(context, levels=levels))
        assert [lv.price for lv in picked] == [inside]

    def test_no_candles_means_no_levels(self) -> None:
        context = replace(
            _windowed_context(), candles=[], levels=[_level(D("100"), resistance=True)]
        )
        assert _nearby_levels(context) == []

    def test_missing_atr_means_zero_margin(self) -> None:
        context = replace(_windowed_context(), atr=None)
        levels = [_level(D("110.5"), resistance=True), _level(D("109"), resistance=True)]
        picked = _nearby_levels(replace(context, levels=levels))
        assert [lv.price for lv in picked] == [D("109")]  # окно [90; 110] без запаса

    def test_far_level_is_not_drawn_on_the_chart(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Регрессия: ось Y не должна растягиваться до уровня вне окна свечей."""
        drawn: list[float] = []
        real = Axes.axhline

        def spy(self, y=0, *args, **kwargs):  # type: ignore[no-untyped-def]
            drawn.append(float(y))
            return real(self, y, *args, **kwargs)

        monkeypatch.setattr(Axes, "axhline", spy)
        context = replace(
            _windowed_context(),
            levels=[_level(D("105"), resistance=True), _level(D("60"), resistance=False)],
        )
        signal = Signal(
            symbol="BTC-USDT", timeframe="1h", direction=SignalDirection.WAIT, setup="Нет сетапа"
        )
        assert render_analysis_chart(context, signal) is not None
        assert 105.0 in drawn
        assert 60.0 not in drawn

    def test_title_does_not_claim_scanner_status(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        titles: list[str] = []
        real = charting._render

        def spy(context, signal, label, **kwargs):  # type: ignore[no-untyped-def]
            titles.append(label)
            return real(context, signal, label, **kwargs)

        monkeypatch.setattr(charting, "_render", spy)
        render_analysis_chart(_context(), _signal())
        assert titles and "READY" not in titles[0]
        assert "по запросу" in titles[0]

    def test_broken_context_returns_none_not_raises(self) -> None:
        assert render_analysis_chart(_context(candles=[]), _signal()) is None

    def test_concurrent_renders_do_not_interfere(self) -> None:
        """Рендеры из разных потоков (сканер + хендлер бота) сериализуются:
        pyplot держит глобальное состояние и не потокобезопасен."""
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: render_analysis_chart(_context(), _signal()), range(6))
            )
        assert all(r is not None and r[:8] == PNG_MAGIC for r in results)


class TestTimeAxis:
    """Подписи оси времени: короткий формат без года и запятой, ограниченное
    число делений. Одинаково для графика сканера и графика экрана анализа."""

    @staticmethod
    def _labels(monkeypatch, render) -> list[str]:  # type: ignore[no-untyped-def]
        figures = []
        real_close = charting.plt.close
        monkeypatch.setattr(
            charting.plt, "close", lambda fig=None: (figures.append(fig), real_close(fig))[1]
        )
        assert render() is not None
        assert figures, "фигура не была закрыта — нечего проверять"
        axis = figures[0].axes[0]
        return [t.get_text() for t in axis.get_xticklabels() if t.get_text()]

    @pytest.mark.parametrize(
        "render",
        [
            lambda: render_setup_chart(_context(), _signal(), SignalLevel.READY),
            lambda: render_analysis_chart(_context(), _signal()),
        ],
        ids=["scanner", "analysis"],
    )
    def test_short_format_and_tick_limit(self, monkeypatch, render) -> None:  # type: ignore[no-untyped-def]
        labels = self._labels(monkeypatch, render)
        assert 2 <= len(labels) <= charting.XAXIS_MAX_TICKS
        for label in labels:
            # 17.09 08:00 — без года и запятой
            assert re.fullmatch(r"\d{2}\.\d{2} \d{2}:\d{2}", label), label


def _stepped_context(low_close: str = "100", high_close: str = "200") -> MarketContext:
    """Первые 200 свечей у low_close, последние 100 у high_close: EMA200 после
    скачка отстаёт и остаётся ниже окна видимых свечей, EMA50 догоняет."""
    now = datetime.now(UTC)
    candles = []
    for i in range(300):
        close = D(low_close) if i < 200 else D(high_close)
        open_time = now - timedelta(hours=300 - i)
        candles.append(
            Kline(
                open_time=open_time, open=close, high=close + D("1"), low=close - D("1"),
                close=close, volume=D("1000"), close_time=open_time + timedelta(hours=1),
            )
        )
    return _context(candles)


def _spy_addplot(monkeypatch) -> list[list[float]]:  # type: ignore[no-untyped-def]
    plotted: list[list[float]] = []
    real = charting.mpf.make_addplot

    def spy(data, *args, **kwargs):  # type: ignore[no-untyped-def]
        plotted.append(list(data))
        return real(data, *args, **kwargs)

    monkeypatch.setattr(charting.mpf, "make_addplot", spy)
    return plotted


class TestVisibleWindowLines:
    def test_clip_replaces_out_of_window_values_with_none(self) -> None:
        bounds = (D("10"), D("20"))
        values = [D("9.9"), D("10"), D("15"), D("20"), D("20.1"), None]
        expected = [None, D("10"), D("15"), D("20"), None, None]
        assert charting._clip_to_bounds(values, bounds) == expected

    def test_clip_without_window_keeps_values(self) -> None:
        values = [D("1"), None]
        assert charting._clip_to_bounds(values, None) == values

    def test_bounds_are_window_plus_one_atr(self) -> None:
        context = _windowed_context()  # свечи [90; 110], ATR 1.5
        assert charting._visible_bounds(context) == (D("88.5"), D("111.5"))

    def test_line_below_window_is_dropped_not_stretching_axis(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """EMA200 целиком под окном свечей в mplfinance не передаётся, а
        EMA50 (частично в окне) передаётся, обрезанная по окну."""
        plotted = _spy_addplot(monkeypatch)
        assert render_analysis_chart(_stepped_context(), _signal()) is not None

        assert len(plotted) == 1  # только EMA50; EMA200 (~100..163) ниже окна ~[198; 202]
        ema50 = plotted[0]
        assert any(v != v for v in ema50)  # начало обрезано (NaN)
        assert any(v == v for v in ema50)  # но линия не выключена
        assert all(v != v or 197 <= v <= 203 for v in ema50)

    def test_lines_inside_window_are_untouched(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        plotted = _spy_addplot(monkeypatch)
        # 300 свечей: EMA200 прогрета на всём окне, NaN могут дать только обрезка
        assert render_analysis_chart(_context(_candles(300)), _signal()) is not None
        assert len(plotted) == 2
        assert all(v == v for line in plotted for v in line)  # ни одного NaN

    def test_scanner_chart_gets_the_same_clipping(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        plotted = _spy_addplot(monkeypatch)
        assert render_setup_chart(_stepped_context(), _signal(), SignalLevel.READY) is not None
        assert len(plotted) == 1


class TestLegend:
    @staticmethod
    def _geometry(monkeypatch, render):  # type: ignore[no-untyped-def]
        figures = []
        real_close = charting.plt.close
        monkeypatch.setattr(
            charting.plt, "close", lambda fig=None: (figures.append(fig), real_close(fig))[1]
        )
        assert render() is not None
        fig = figures[0]
        canvas = FigureCanvasAgg(fig)  # у закрытой фигуры canvas базовый, без рендерера
        canvas.draw()
        ax = fig.axes[0]
        renderer = canvas.get_renderer()
        return ax.get_window_extent(renderer), ax.get_legend().get_window_extent(renderer)

    def test_analysis_legend_is_below_the_plot(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        context = replace(_windowed_context(), levels=[_level(D("105"), resistance=True)])
        axes_box, legend_box = self._geometry(
            monkeypatch, lambda: render_analysis_chart(context, _signal())
        )
        assert legend_box.y1 <= axes_box.y0, "легенда не должна перекрывать область свечей"

    def test_scanner_legend_stays_inside_top_left(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        axes_box, legend_box = self._geometry(
            monkeypatch,
            lambda: render_setup_chart(_context(), _signal(), SignalLevel.READY),
        )
        assert legend_box.y0 >= axes_box.y0
        assert legend_box.x0 <= axes_box.x0 + axes_box.width / 2
