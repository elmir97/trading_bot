"""Тесты графика экрана «Анализ рынка» (app/analysis/charting.py).

render_analysis_chart не бросает исключения наружу ни при каких входных
данных — экран уходит текстом, если картинка не построилась. 02.10.2026:
сигналы удалены — на графике только рынок: свечи, EMA50/200, ближайшие
уровни, без зоны входа, стопа и цели.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg

from app.analysis import charting
from app.analysis.charting import _nearby_levels, render_analysis_chart
from app.analysis.context import MarketContext
from app.analysis.structure import Level
from app.exchanges.base import Kline
from app.trading.enums import MarketStructure

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
    def test_returns_valid_png(self) -> None:
        png = render_analysis_chart(_context())
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_with_nearby_levels_renders(self) -> None:
        context = replace(
            _windowed_context(),
            levels=[
                _level(D("105"), resistance=True),
                _level(D("108"), resistance=True),
                _level(D("95"), resistance=False),
            ],
        )
        png = render_analysis_chart(context)
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
        assert render_analysis_chart(context) is not None
        assert 105.0 in drawn
        assert 60.0 not in drawn

    def test_no_signal_overlays(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Сигналы удалены: ни зоны входа (axhspan), ни линий стоп/цель."""
        spans: list[object] = []
        labels: list[str] = []
        real_hline = Axes.axhline

        def hline(self, y=0, *args, **kwargs):  # type: ignore[no-untyped-def]
            labels.append(kwargs.get("label", ""))
            return real_hline(self, y, *args, **kwargs)

        monkeypatch.setattr(Axes, "axhline", hline)
        monkeypatch.setattr(Axes, "axhspan", lambda self, *a, **k: spans.append(a))
        context = replace(_windowed_context(), levels=[_level(D("105"), resistance=True)])
        assert render_analysis_chart(context, 2) is not None
        assert spans == []
        assert all(label.startswith(("Сопротивление", "Поддержка")) for label in labels)

    def test_title_is_symbol_and_timeframe_only(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        titles: list[str] = []
        real = charting.mpf.plot

        def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
            titles.append(kwargs.get("title", ""))
            return real(*args, **kwargs)

        monkeypatch.setattr(charting.mpf, "plot", spy)
        assert render_analysis_chart(_context()) is not None
        assert titles == ["BTC-USDT · 1H"]

    def test_broken_context_returns_none_not_raises(self) -> None:
        """Пустые свечи ломают расчёт EMA — рендер обязан проглотить это
        и вернуть None, а не уронить вызывающий код."""
        assert render_analysis_chart(_context(candles=[])) is None

    def test_concurrent_renders_do_not_interfere(self) -> None:
        """Рендеры из разных потоков сериализуются: pyplot держит глобальное
        состояние и не потокобезопасен."""
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: render_analysis_chart(_context()), range(6)))
        assert all(r is not None and r[:8] == PNG_MAGIC for r in results)


class TestTimeAxis:
    """Подписи оси времени: короткий формат без года и запятой, ограниченное
    число делений."""

    def test_short_format_and_tick_limit(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        figures = []
        real_close = charting.plt.close
        monkeypatch.setattr(
            charting.plt, "close", lambda fig=None: (figures.append(fig), real_close(fig))[1]
        )
        assert render_analysis_chart(_context()) is not None
        assert figures, "фигура не была закрыта — нечего проверять"
        labels = [t.get_text() for t in figures[0].axes[0].get_xticklabels() if t.get_text()]
        assert 2 <= len(labels) <= charting.XAXIS_MAX_TICKS
        for label in labels:
            # 17.09 08:00 — без года и запятой
            assert re.fullmatch(r"\d{2}\.\d{2} \d{2}:\d{2}", label), label


def _spy_addplot(monkeypatch) -> list[list[float]]:  # type: ignore[no-untyped-def]
    plotted: list[list[float]] = []
    real = charting.mpf.make_addplot

    def spy(data, *args, **kwargs):  # type: ignore[no-untyped-def]
        plotted.append(list(data))
        return real(data, *args, **kwargs)

    monkeypatch.setattr(charting.mpf, "make_addplot", spy)
    return plotted


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
        assert render_analysis_chart(_stepped_context()) is not None

        assert len(plotted) == 1  # только EMA50; EMA200 (~100..163) ниже окна ~[198; 202]
        ema50 = plotted[0]
        assert any(v != v for v in ema50)  # начало обрезано (NaN)
        assert any(v == v for v in ema50)  # но линия не выключена
        assert all(v != v or 197 <= v <= 203 for v in ema50)

    def test_lines_inside_window_are_untouched(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        plotted = _spy_addplot(monkeypatch)
        # 300 свечей: EMA200 прогрета на всём окне, NaN могут дать только обрезка
        assert render_analysis_chart(_context(_candles(300))) is not None
        assert len(plotted) == 2
        assert all(v == v for line in plotted for v in line)  # ни одного NaN


class TestLegend:
    def test_legend_is_below_the_plot(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        figures = []
        real_close = charting.plt.close
        monkeypatch.setattr(
            charting.plt, "close", lambda fig=None: (figures.append(fig), real_close(fig))[1]
        )
        context = replace(_windowed_context(), levels=[_level(D("105"), resistance=True)])
        assert render_analysis_chart(context) is not None
        fig = figures[0]
        canvas = FigureCanvasAgg(fig)  # у закрытой фигуры canvas базовый, без рендерера
        canvas.draw()
        renderer = canvas.get_renderer()
        axes_box = fig.axes[0].get_window_extent(renderer)
        legend_box = fig.axes[0].get_legend().get_window_extent(renderer)
        assert legend_box.y1 <= axes_box.y0, "легенда не должна перекрывать область свечей"

    def test_no_levels_no_legend(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        figures = []
        real_close = charting.plt.close
        monkeypatch.setattr(
            charting.plt, "close", lambda fig=None: (figures.append(fig), real_close(fig))[1]
        )
        assert render_analysis_chart(_context()) is not None
        assert figures[0].axes[0].get_legend() is None


class TestPriceLabels:
    """Подписи легенды на картинке: fmt_price по точности символа, а не {x:.4f}."""

    RAW = re.compile(r"(?<![\d.])\d+\.\d{4}(?!\d)")

    @staticmethod
    def _labels(monkeypatch, render) -> list[str]:  # type: ignore[no-untyped-def]
        labels: list[str] = []
        real_hline = Axes.axhline

        def hline(self, y=0, *args, **kwargs):  # type: ignore[no-untyped-def]
            labels.append(kwargs.get("label", ""))
            return real_hline(self, y, *args, **kwargs)

        monkeypatch.setattr(Axes, "axhline", hline)
        assert render() is not None
        return labels

    def test_labels_use_symbol_precision(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        context = replace(_windowed_context(), levels=[_level(D("105.1234"), resistance=True)])
        labels = self._labels(monkeypatch, lambda: render_analysis_chart(context, 2))
        assert "Сопротивление 105.12" in labels
        assert not [x for x in labels if self.RAW.search(x)]

    def test_other_precision_is_respected(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        context = replace(_windowed_context(), levels=[_level(D("105.1234"), resistance=True)])
        labels = self._labels(monkeypatch, lambda: render_analysis_chart(context, 3))
        assert "Сопротивление 105.123" in labels

    def test_role_follows_price_not_detection_flag(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Подпись — по положению относительно цены (~100): пробитое
        «сопротивление» ниже цены — поддержка, и наоборот."""
        context = replace(
            _windowed_context(),
            levels=[
                _level(D("95"), resistance=True),
                _level(D("105"), resistance=False),
            ],
        )
        labels = self._labels(monkeypatch, lambda: render_analysis_chart(context, 2))
        assert "Поддержка 95" in labels
        assert "Сопротивление 105" in labels
        assert "Сопротивление 95" not in labels
        assert "Поддержка 105" not in labels
