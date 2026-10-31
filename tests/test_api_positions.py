"""API Mini App, П.1: GET /api/positions — только чтение, счёт Mini App, связь с
журналом без сделок NULL и другого счёта, кэш 5 с, состояния без ключа / сбоя биржи;
bot_username в /api/me (getMe при старте, сбой — API всё равно поднимается)."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from app.api import main as api_main
from app.api import positions as positions_api
from app.api.deps import ApiState
from app.database.models.execution_callback import ExecutionCallback
from app.database.models.execution_order import ExecutionOrder
from app.database.models.trade import Trade
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import UserSettings
from app.exchanges.base import (
    ExchangeAuthError,
    ExchangeUnavailableError,
    OpenOrder,
    Position,
)
from app.trading.enums import ExchangeKeyMode, TradeSide, TradeSource
from tests.api_helpers import (  # noqa: F401 — api_fixture: фикстура «api»
    FakeFactory,
    FakePublic,
    api_fixture,
    auth,
    client_for,
)

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
DEMO, LIVE = ExchangeKeyMode.DEMO, ExchangeKeyMode.LIVE


def _position(side: TradeSide = TradeSide.LONG, **kw: Any) -> Position:
    fields: dict[str, Any] = {
        "symbol": "XRP-USDT", "side": side, "quantity": D(195), "entry_price": D("1.38184"),
        "mark_price": D("1.38211"), "leverage": 10, "unrealized_pnl": D("0.05265"),
        "liquidation_price": D("1.25003"), "position_id": "2105907655281221634",
    }
    fields.update(kw)
    return Position(**fields)


def _order(order_type: str, stop: str, *, side: str = "SELL", position_side: str = "LONG",
           close_position: bool = True, order_id: str = "2105910661355233280") -> OpenOrder:
    return OpenOrder(
        order_id=order_id, client_order_id="", symbol="XRP-USDT", side=side,
        position_side=position_side, order_type=order_type, quantity=D(195),
        executed_qty=D(0), price=D(0), stop_price=D(stop), status="NEW", leverage=10,
        reduce_only=True, close_position=close_position, working_type="MARK_PRICE",
        created_at=NOW, updated_at=NOW, take_profit=None, stop_loss=None,
    )


class PositionsClient:
    """Ключевой клиент: позиции и openOrders; calls — по методам, symbol openOrders."""

    def __init__(self, positions: list[Position] | None = None,
                 orders: list[OpenOrder] | None = None, fail: Exception | None = None) -> None:
        self.positions = positions or []
        self.orders = orders or []
        self.fail = fail
        self.calls = {"positions": 0, "open_orders": 0}
        self.order_symbols: list[str | None] = []
        self.closed = 0

    async def get_positions(self, *, max_attempts: int | None = None) -> list[Position]:
        self.calls["positions"] += 1
        if self.fail is not None:
            raise self.fail
        return list(self.positions)

    async def get_open_orders(self, symbol: str | None = None, *,
                              max_attempts: int | None = None) -> list[OpenOrder]:
        self.calls["open_orders"] += 1
        self.order_symbols.append(symbol)
        return list(self.orders)

    async def place_order(self, **kw: Any) -> None:     # запись — не должна быть доступна
        raise AssertionError("запись на биржу из «Позиций»")

    async def close(self) -> None:
        self.closed += 1


async def _trade(api: Any, *, side: TradeSide = TradeSide.LONG,
                 mode: ExchangeKeyMode | None = DEMO, hours: int = 1,
                 position_id: str | None = None, symbol: str = "XRP-USDT",
                 entry: str = "1.3818") -> int:
    state = api.state()
    async with state.db.session() as session:
        trade = Trade(
            user_id=api.uid, symbol=symbol, side=side, entry_price=D(entry),
            quantity=D(195), source=TradeSource.IMPORTED if mode else TradeSource.MANUAL,
            account_mode=mode, opened_at=datetime.now(UTC) - timedelta(hours=hours),
            external_position_id=position_id,
        )
        session.add(trade)
        await session.commit()
        return trade.id


async def _get(state: ApiState, api: Any, **kw: Any) -> Any:
    async with client_for(state) as c:
        return await c.get("/api/positions", headers=auth(api.tid, **kw))


async def test_positions_linked_unlinked_and_journal(api) -> None:  # type: ignore[no-untyped-def]
    """Хедж XRP: LONG связан с DEMO-сделкой, SHORT — нет (у него только ручная
    запись без счёта и LIVE-сделка). Обе — «только в журнале» с пометкой счёта."""
    linked = await _trade(api)
    manual = await _trade(api, side=TradeSide.SHORT, mode=None)
    live = await _trade(api, side=TradeSide.SHORT, mode=LIVE)
    client = PositionsClient(
        [_position(), _position(TradeSide.SHORT, liquidation_price=D(0))],
        [_order("STOP_MARKET", "1.34021"), _order("STOP_MARKET", "1.3", order_id="2"),
         _order("TAKE_PROFIT_MARKET", "1.4231", close_position=False, order_id="3")],
    )
    r = await _get(api.state(factory=FakeFactory(client)), api)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_mode"] == "DEMO" and body["exchange_error"] is None
    long, short = body["positions"]
    assert long == {
        "symbol": "XRP-USDT", "side": "LONG", "quantity": "195", "leverage": 10,
        "entry_price": "1.3818", "mark_price": "1.3821", "unrealized_pnl": "0.05",
        "pnl_text": "+0.05 $", "pnl_sign": 1,
        "liquidation_price": "1.2500", "price_precision": 4,
        # Д.6: полоска от ближнего стопа 1.34021 к ближнему тейку 1.4231 — по сырым ценам
        # биржи, подписи по точности контракта
        "bar": {"stop": "стоп 1.3402", "entry": "вход 1.3818", "take": "тейк 1.4231",
                "mark": "1.3821", "entry_at": "0.5022", "mark_at": "0.5055", "mark_edge": None},
        # лестница: от ближнего к цене; closePosition — без объёма
        "stops": [{"price": "1.3402", "close_position": True, "quantity": None},
                  {"price": "1.3000", "close_position": True, "quantity": None}],
        "takes": [{"price": "1.4231", "close_position": False, "quantity": "195"}],
        "trade_id": linked, "start_param": "pos-XRP-USDT-L",
    }
    assert short["trade_id"] is None and short["stops"] == [] and short["takes"] == []
    assert short["bar"] is None                          # нет стопа и тейка — полоски нет
    assert short["liquidation_price"] is None and short["start_param"] == "pos-XRP-USDT-S"
    journal = {t["id"]: t["account_mode"] for t in body["journal_only"]}
    assert journal == {manual: None, live: "LIVE"}
    assert client.order_symbols == [None]                # один openOrders на все символы
    assert client.closed == 1


async def test_null_and_other_account_never_link(api) -> None:  # type: ignore[no-untyped-def]
    """Позиция DEMO, в журнале по тому же символу/стороне — ручная запись NULL и
    LIVE-сделка с тем же positionId: ни одна не связывается."""
    manual = await _trade(api, mode=None, hours=1)
    live = await _trade(api, mode=LIVE, hours=2, position_id="2105907655281221634")
    client = PositionsClient([_position()])
    body = (await _get(api.state(factory=FakeFactory(client)), api)).json()
    assert body["positions"][0]["trade_id"] is None
    assert sorted(t["id"] for t in body["journal_only"]) == sorted([manual, live])


async def test_cache_one_request_per_ttl(api) -> None:  # type: ignore[no-untyped-def]
    client = PositionsClient([_position()], [_order("STOP_MARKET", "1.34")])
    public = FakePublic()
    state = api.state(factory=FakeFactory(client, public=public))
    first = await _get(state, api)
    second = await _get(state, api)
    assert first.json() == second.json()
    assert client.calls == {"positions": 1, "open_orders": 1}
    key = positions_api.cache_key(state, api.uid)
    assert key == f"api:positions:{api.uid}:DEMO"
    left = state.market_cache._data[key].expires_at - time.monotonic()
    assert 4 < left <= positions_api.TTL_POSITIONS == 5
    assert public.calls["symbols"] == 1                  # точность — из кэша контрактов


async def test_no_positions_no_open_orders(api) -> None:  # type: ignore[no-untyped-def]
    client = PositionsClient()
    body = (await _get(api.state(factory=FakeFactory(client)), api)).json()
    assert body["positions"] == [] and body["exchange_error"] is None
    assert client.calls == {"positions": 1, "open_orders": 0}


async def test_no_key_shows_journal_only(api) -> None:  # type: ignore[no-untyped-def]
    trade_id = await _trade(api)
    client = PositionsClient([_position()])
    r = await _get(api.state(factory=FakeFactory(client, has_key=False)), api)
    assert r.status_code == 200
    body = r.json()
    assert body["exchange_error"] == {"code": "NO_KEY", "message": positions_api.NO_KEY_TEXT}
    assert body["positions"] == [] and [t["id"] for t in body["journal_only"]] == [trade_id]
    assert client.calls == {"positions": 0, "open_orders": 0}


async def test_key_rejected_shows_journal_only(api) -> None:  # type: ignore[no-untyped-def]
    client = PositionsClient(fail=ExchangeAuthError("Invalid API-key"))
    body = (await _get(api.state(factory=FakeFactory(client)), api)).json()
    assert body["exchange_error"]["code"] == "KEY_REJECTED" and body["positions"] == []
    assert body["exchange_error"]["message"] == positions_api.KEY_REJECTED_TEXT
    assert "Ключ биржи отклонён — проверь в настройках бота" in positions_api.KEY_REJECTED_TEXT
    assert positions_api.KEY_REJECTED_TEXT != positions_api.NO_KEY_TEXT     # на экране различимы


async def test_exchange_failure_503_not_cached(api) -> None:  # type: ignore[no-untyped-def]
    client = PositionsClient(fail=ExchangeUnavailableError("timeout"))
    state = api.state(factory=FakeFactory(client))
    r = await _get(state, api)
    assert r.status_code == 503
    assert r.json() == {"code": "EXCHANGE_UNAVAILABLE",
                        "message": "Биржа не ответила (ExchangeUnavailableError) — повтори."}
    client.fail = None
    assert (await _get(state, api)).status_code == 200    # сбой не закэширован
    assert client.calls["positions"] == 2


async def test_unauthorized(api) -> None:  # type: ignore[no-untyped-def]
    async with client_for(api.state()) as c:
        assert (await c.get("/api/positions")).status_code == 401
    assert (await _get(api.state(), api, auth_date=int(time.time()) - 7200)).status_code == 401


@pytest.mark.parametrize("bot_mode", [DEMO, LIVE])
async def test_bot_account_shown(api, bot_mode) -> None:  # type: ignore[no-untyped-def]
    """Счёт чтения совпадает с выбранным в боте; legacy-поле сохранено в API."""
    state = api.state(factory=FakeFactory(PositionsClient()))
    async with state.db.session() as session:
        st = await session.scalar(select(UserSettings).where(UserSettings.user_id == api.uid))
        assert st is not None
        st.active_exchange_mode = bot_mode
        await session.commit()
    body = (await _get(state, api)).json()
    assert (body["account_mode"], body["bot_account_mode"]) == (bot_mode.value, bot_mode.value)


async def test_cache_per_user_and_forget(api) -> None:  # type: ignore[no-untyped-def]
    state = api.state()
    state.market_cache.set(f"api:positions:{api.uid}:DEMO", "mine", 60)
    state.market_cache.set(f"api:positions:{api.uid + 1}:DEMO", "other", 60)
    positions_api.forget(state, api.uid)
    assert state.market_cache.get(f"api:positions:{api.uid}:DEMO") is None
    assert state.market_cache.get(f"api:positions:{api.uid + 1}:DEMO") == "other"


async def test_reads_only(api) -> None:  # type: ignore[no-untyped-def]
    client = PositionsClient([_position()])
    reads = positions_api.ExchangeReads(client)
    with pytest.raises(AttributeError, match="не пишут на биржу: place_order"):
        _ = reads.place_order
    assert (await reads.get_positions())[0].symbol == "XRP-USDT"
    # ручка в базу ничего не пишет
    state = api.state(factory=FakeFactory(client))
    models = (Trade, TradeOpening, ExecutionOrder, ExecutionCallback)

    async def counts() -> list[int]:
        async with state.db.session() as session:
            return [int(await session.scalar(select(func.count()).select_from(m)) or 0)
                    for m in models]

    before = await counts()
    assert (await _get(state, api)).status_code == 200
    assert await counts() == before


# --- имя бота для ссылки в чат -------------------------------------------------------------


async def test_me_bot_username(api) -> None:  # type: ignore[no-untyped-def]
    state = api.state()
    async with client_for(state) as c:
        assert (await c.get("/api/me", headers=auth(api.tid))).json()["bot_username"] is None
        state.bot_username = "postfactumTrade_bot"
        assert (await c.get("/api/me", headers=auth(api.tid))).json()["bot_username"] \
            == "postfactumTrade_bot"


class _Bot:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.session = SimpleNamespace(close=self._close)
        self.closed = False

    async def get_me(self) -> Any:
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(username="postfactumTrade_bot")

    async def _close(self) -> None:
        self.closed = True


async def test_read_bot_username(caplog) -> None:  # type: ignore[no-untyped-def]
    assert await api_main.read_bot_username(_Bot()) == "postfactumTrade_bot"  # type: ignore[arg-type]
    caplog.set_level(logging.WARNING)
    assert await api_main.read_bot_username(_Bot(TimeoutError())) is None  # type: ignore[arg-type]
    assert "getMe не прочитан" in caplog.text


async def test_api_starts_when_get_me_fails(api, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Сбой getMe при старте — API поднимается, bot_username=None, кнопки в чат нет."""
    bot = _Bot(ConnectionError("telegram down"))
    built = SimpleNamespace(bot=bot, confirm_tasks={}, action_tasks={}, bot_username="stale",
                            redis=SimpleNamespace(aclose=_noop), db=SimpleNamespace(dispose=_noop),
                            factory=SimpleNamespace(aclose=_noop))
    worker = SimpleNamespace(close=_noop)
    monkeypatch.setattr(api_main, "get_settings", lambda: api.settings)
    monkeypatch.setattr(api_main, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(api_main, "build_state", lambda settings: (built, worker))
    app = api_main.create_app()
    async with app.router.lifespan_context(app):
        assert app.state.api is built and built.bot_username is None
    assert bot.closed


async def _noop() -> None:
    return None


async def test_journal_entry_by_contract_precision(api) -> None:  # type: ignore[no-untyped-def]
    """Д.6: «Только в журнале» — вход по точности контракта, не «2400.000000000000»;
    контракта нет в списке — без хвоста нулей. И с ключом (список читается для позиций),
    и без ключа (список публичный)."""
    xrp = await _trade(api, mode=None, entry="1.381850000000")
    btc = await _trade(api, mode=LIVE, symbol="BTC-USDT", entry="62000.000000000000")
    eth = await _trade(api, mode=LIVE, symbol="ETH-USDT", entry="2400.000000000000")
    want = {xrp: "1.3819", btc: "62000.0", eth: "2400"}
    for has_key in (True, False):
        client = PositionsClient([])
        body = (await _get(api.state(factory=FakeFactory(client, has_key=has_key)), api)).json()
        got = {t["id"]: t["entry_price"] for t in body["journal_only"]}
        assert got == want, has_key


async def test_journal_survives_contract_list_failure(api) -> None:  # type: ignore[no-untyped-def]
    """Список контрактов не прочитан — вкладка не падает: вход журнала без хвоста нулей."""
    trade_id = await _trade(api, mode=None, entry="1.381800000000")
    public = FakePublic()
    public.fail["symbols"] = ExchangeUnavailableError("timeout")
    r = await _get(api.state(factory=FakeFactory(PositionsClient([]), public=public)), api)
    assert r.status_code == 200, r.text
    assert [(t["id"], t["entry_price"]) for t in r.json()["journal_only"]] == [(trade_id, "1.3818")]
