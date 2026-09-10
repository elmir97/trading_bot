"""Тесты сверки инструментов плана со списком биржи.

Реальный случай: Toncoin в API BingX (/openApi/swap/v2/quote/contracts)
торгуется под тикером GRAMTON-USDT (поле asset = "GRAMTON"), а не
TON-USDT. «GRAM-USDT» — это только displayName, которым биржа подписывает
эту пару в своём интерфейсе; самого символа с таким именем среди
контрактов нет, и сверяться нужно с тем, что отдаёт API, а не с тем, что
показывает сайт. Неверный тикер ничего не роняет — инструмент просто
молча исчезает из цен и анализа, и заметить это можно только вручную.
«GRAMTON» и «TON» не пересекаются по началу строки, поэтому эвристика по
общему префиксу этот случай не ловит — нужен явный алиас.
"""

from __future__ import annotations

from app.bot.handlers.exchange import _base_asset, _match_symbols

EXCHANGE = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "GRAMTON-USDT",
]


def test_base_asset() -> None:
    assert _base_asset("BTC-USDT") == "BTC"
    assert _base_asset("gramton-usdt") == "GRAMTON"


def test_all_matching() -> None:
    matched, fixable, missing = _match_symbols(["BTC-USDT", "ETH-USDT"], EXCHANGE)
    assert matched == ["BTC-USDT", "ETH-USDT"]
    assert fixable == {}
    assert missing == []


def test_renamed_ticker_is_offered_for_fix() -> None:
    """Тот самый случай с Toncoin: TON-USDT → GRAMTON-USDT через алиас."""
    matched, fixable, missing = _match_symbols(["BTC-USDT", "TON-USDT"], EXCHANGE)
    assert matched == ["BTC-USDT"]
    assert fixable == {"TON-USDT": "GRAMTON-USDT"}
    assert missing == []


def test_ticker_matched_by_base_asset() -> None:
    """Когда базовый актив совпадает, подсказываем правильное имя."""
    matched, fixable, missing = _match_symbols(["BTC-USD", "ETH-USDT"], EXCHANGE)
    assert fixable == {"BTC-USD": "BTC-USDT"}
    assert matched == ["ETH-USDT"]
    assert missing == []


def test_delisted_symbol_reported_as_missing() -> None:
    matched, fixable, missing = _match_symbols(["LUNA-USDT"], EXCHANGE)
    assert missing == ["LUNA-USDT"]
    assert fixable == {}


def test_empty_plan() -> None:
    assert _match_symbols([], EXCHANGE) == ([], {}, [])


def test_default_symbols_use_bingx_names() -> None:
    """Список по умолчанию должен совпадать с тикерами BingX.

    Регрессия в два захода: изначально там был TON-USDT, потом его
    заменили на TONCOIN-USDT, потом на GRAM-USDT (по displayName из
    интерфейса биржи) — и всё это мимо реального тикера API, GRAMTON-USDT.
    Во всех случаях инструмент молча выпадал из цен.
    """
    from app.database.models.trading_plan import DEFAULT_ALLOWED_SYMBOLS

    matched, fixable, missing = _match_symbols(DEFAULT_ALLOWED_SYMBOLS, EXCHANGE)
    assert not fixable
    assert not missing
    assert len(matched) == len(DEFAULT_ALLOWED_SYMBOLS)


def test_alias_used_only_when_direct_match_missing() -> None:
    """Алиас — это подсказка на крайний случай, а не приоритет над точным
    совпадением: если TON-USDT реально есть на бирже, менять его не на что."""
    matched, fixable, missing = _match_symbols(["TON-USDT"], [*EXCHANGE, "TON-USDT"])
    assert matched == ["TON-USDT"]
    assert fixable == {}
    assert missing == []


def test_shortened_ticker_matched_when_unambiguous() -> None:
    """Биржа использует более длинное имя, но кандидат единственный."""
    matched, fixable, missing = _match_symbols(["DOG-USDT"], ["DOGE-USDT"])
    assert fixable == {"DOG-USDT": "DOGE-USDT"}
    assert missing == []


def test_ambiguous_prefix_is_not_guessed() -> None:
    """Если кандидатов несколько, подменять нельзя.

    «ETH» подходит и к ETH, и к ETHFI. Молча выбрать один — значит
    отправить пользователя торговать не тем инструментом.
    """
    available = ["ETHFI-USDT", "ETHW-USDT"]
    matched, fixable, missing = _match_symbols(["ETH-USDT"], available)
    assert fixable == {}
    assert missing == ["ETH-USDT"]


def test_exact_match_wins_over_prefix() -> None:
    available = ["ETH-USDT", "ETHFI-USDT"]
    matched, fixable, missing = _match_symbols(["ETH-USDT"], available)
    assert matched == ["ETH-USDT"]
    assert fixable == {}
