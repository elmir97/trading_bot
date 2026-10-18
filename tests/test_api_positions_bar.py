"""Экран позиций Mini App (Д.6): полоска стоп — вход — тейк, PnL и вход журнала считает и
форматирует сервер (app/api/positions.py); фронт только ставит left:% и цвет по знаку."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.api import positions as positions_api
from app.trading.enums import TradeSide

D = Decimal
LONG, SHORT = TradeSide.LONG, TradeSide.SHORT


def _bar(side: TradeSide, entry: str, mark: str, stop: str | None, take: str | None,
         precision: int | None = 4) -> positions_api.BarOut | None:
    return positions_api.position_bar(side, D(entry), D(mark), None if stop is None else D(stop),
                                      None if take is None else D(take), precision)


# --- полоска -------------------------------------------------------------------------------


def test_long_bar_from_stop_to_take() -> None:
    bar = _bar(LONG, "1.4810", "1.4950", "1.4402", "1.5631")
    assert bar is not None
    assert (bar.stop, bar.entry, bar.take, bar.mark) == ("стоп 1.4402", "вход 1.4810",
                                                         "тейк 1.5631", "1.4950")
    # (1.4810 − 1.4402) / (1.5631 − 1.4402) = 0.33198…; цена: 0.44589…
    assert (bar.entry_at, bar.mark_at, bar.mark_edge) == (D("0.3320"), D("0.4459"), None)


def test_short_bar_is_mirrored_stop_left_take_right() -> None:
    """Шорт: стоп выше входа, тейк ниже — та же шкала «стоп слева, тейк справа»: цена,
    ушедшая вниз (к тейку), — правее входа."""
    bar = _bar(SHORT, "1.5120", "1.4950", "1.5500", "1.4500")
    assert bar is not None
    assert (bar.entry_at, bar.mark_at, bar.mark_edge) == (D("0.3800"), D("0.5500"), None)
    assert bar.mark_at > bar.entry_at
    long = _bar(LONG, "1.4880", "1.5050", "1.4500", "1.5500")
    assert long is not None and long.mark_at > long.entry_at   # к тейку — вправо у обеих


@pytest.mark.parametrize(("side", "mark", "at", "edge"), [
    (LONG, "1.4000", "0", "stop"),       # лонг: цена ниже стопа
    (LONG, "1.6000", "1", "take"),       # лонг: выше тейка
    (SHORT, "1.6000", "0", "stop"),      # шорт: выше стопа
    (SHORT, "1.4000", "1", "take"),      # шорт: ниже тейка
    (LONG, "1.4402", "0", None),         # ровно на стопе — край, но не «за»
])
def test_mark_beyond_edges_is_clamped(side: TradeSide, mark: str, at: str,
                                      edge: str | None) -> None:
    stop, take = ("1.4402", "1.5631") if side is LONG else ("1.5631", "1.4402")
    bar = _bar(side, "1.4810" if side is LONG else "1.5120", mark, stop, take)
    assert bar is not None
    assert bar.mark_at == D(at).quantize(D("0.0001")) and bar.mark_edge == edge


def test_entry_beyond_edge_is_clamped_too() -> None:
    """Стоп перенесён выше входа (БУ+) — вход левее шкалы: обрезан к 0."""
    bar = _bar(LONG, "1.4810", "1.5000", "1.4900", "1.5631")
    assert bar is not None and bar.entry_at == D("0.0000")


@pytest.mark.parametrize(("side", "stop", "take"), [
    (LONG, None, "1.5631"), (LONG, "1.4402", None), (SHORT, None, None),
    (LONG, "1.5631", "1.4402"),          # лонг: тейк ниже стопа — уровни не по сторонам
    (SHORT, "1.4402", "1.5631"),         # шорт: тейк выше стопа
    (LONG, "1.4402", "1.4402"),          # стоп = тейк — шкалы нет
])
def test_no_bar_without_levels_or_wrong_sides(side: TradeSide, stop: str | None,
                                              take: str | None) -> None:
    assert _bar(side, "1.4810", "1.4950", stop, take) is None


def test_bar_texts_use_contract_precision_or_raw() -> None:
    bar = _bar(LONG, "1.48104", "1.49501", "1.44021", "1.56309", precision=4)
    assert bar is not None and (bar.stop, bar.take, bar.mark) == ("стоп 1.4402", "тейк 1.5631",
                                                                  "1.4950")
    raw = _bar(LONG, "1.48104", "1.49501", "1.44021", "1.56309", precision=None)
    assert raw is not None and raw.stop == "стоп 1.44021"


# --- PnL -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "text", "sign"), [
    ("2.7312", "+2.73 $", 1), ("-0.8466", "-0.85 $", -1), ("0.004", "0.00 $", 0),
    ("-0.004", "0.00 $", 0), (None, None, 0),
])
def test_pnl_text_sign_from_server(value: str | None, text: str | None, sign: int) -> None:
    """Плюс — синий, минус — красный, ноль — muted: знак и текст ставит сервер (по
    округлённому значению: −0.004 → «0.00 $», ноль)."""
    assert positions_api.pnl_text(None if value is None else D(value)) == (text, sign)


# --- вход «Только в журнале» -------------------------------------------------------------


@pytest.mark.parametrize(("value", "precision", "shown"), [
    ("2400.000000000000", 2, "2400.00"),
    ("2400.000000000000", None, "2400"),        # контракта нет в списке — без хвоста нулей
    ("62000.000000000000", 1, "62000.0"),
    ("1.381850000000", 4, "1.3819"),
    ("0.000012340000", None, "0.00001234"),     # не «1.234E-5»
])
def test_journal_entry_price_by_contract_precision(value: str, precision: int | None,
                                                   shown: str) -> None:
    got = positions_api.journal_price(D(value), precision)
    assert got is not None and str(got) == shown
    assert positions_api.journal_price(None, precision) is None
