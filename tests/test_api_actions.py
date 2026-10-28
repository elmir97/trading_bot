"""API Mini App, экран 5 (5.1): действия с позицией — экран действий, превью,
карточка, «Да» / «Нет», статус — против настоящей БД, FakeRedis и фейковой биржи с
состоянием из тестов ядра (tests/test_position_action_service.py)."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api import actions as actions_api
from app.api import positions as positions_api
from app.api import ratelimit
from app.api.deps import ApiState
from app.bot import outbox
from app.core.config import Settings
from app.core.locks import RedisLock, position_lock_key
from app.database.models.execution_callback import ExecutionCallback
from app.database.models.outgoing_message import OutgoingMeta
from app.database.models.position_action import PositionAction
from app.database.models.user import User
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.execution.position_action_service import PositionActionService
from app.execution.position_actions import (
    ActionPlan,
    breakeven_price,
    breakeven_threshold,
    plan_action,
    render_card,
)
from app.market.cache import TTLCache
from app.services.user_service import UserService
from app.trading.enums import (
    ExchangeKeyMode,
    PositionActionKind,
    PositionActionStatus,
    TradeSide,
)
from tests.api_helpers import TOKEN, auth, client_for
from tests.conftest import cleanup_user
from tests.test_position_action_service import ENTRY, FakeExchange, _open_order

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)
WRITES = ("place_conditional", "market", "cancel")
BE = {"symbol": "XRP-USDT", "side": "LONG", "kind": "be"}
C25 = {"symbol": "XRP-USDT", "side": "LONG", "kind": "c25"}


class Factory:
    def __init__(self, exchange: FakeExchange) -> None:
        self.exchange = exchange
        self.calls = 0

    async def get_credentials(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        return SimpleNamespace(is_read_only=False, permissions_checked_at=NOW, user_id=user_id,
                               mode=mode)

    async def for_user(self, session, user_id, exchange_name="bingx", mode=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.exchange


async def _make_user(db: Database, settings: Settings, telegram_id: int) -> int:
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=telegram_id)
        user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
        await session.commit()
        return user.id


@pytest_asyncio.fixture
async def ctx(unique_telegram_id, monkeypatch):  # type: ignore[no-untyped-def]
    tid, other_tid = unique_telegram_id(), unique_telegram_id()
    settings = Settings(  # type: ignore[call-arg]
        bot_token=TOKEN, trading_execution_enabled=True, bingx_trading_mode="demo",
        exec_dry_run=False, exec_order_readback_delay_ms=0, miniapp_actions_enabled=True,
        miniapp_allowed_telegram_ids=f"{tid},{other_tid}",
    )
    db = Database(settings)
    uid = await _make_user(db, settings, tid)
    other_uid = await _make_user(db, settings, other_tid)
    exchange = FakeExchange()
    factory = Factory(exchange)
    redis = FakeRedis(decode_responses=True)
    state = ApiState(settings=settings, db=db, redis=redis, factory=factory, bot=object())
    sent: list[dict[str, Any]] = []

    async def fake_send(bot: Any, db_: Any, meta: OutgoingMeta, chat_id: int, text: str,
                        reply_markup: Any = None) -> None:
        sent.append({"chat_id": chat_id, "text": text, "meta": meta, "markup": reply_markup})
        await outbox.record(db_, meta, chat_id=chat_id, message_id=900 + len(sent), text=text)

    monkeypatch.setattr(outbox, "send", fake_send)
    clock = {"t": 1_800_000_000}
    monkeypatch.setattr(ratelimit, "window", lambda: clock["t"])

    async with db.session() as session:

        async def chat_service(user_id: int = uid) -> PositionActionService:
            """Сервис как у кнопок чата (своя сессия теста)."""
            user = await session.scalar(
                select(User).where(User.id == user_id).options(selectinload(User.settings))
            )
            assert user is not None
            return PositionActionService(session, settings, None, user, redis=redis,
                                         factory=Factory(exchange), market_cache=TTLCache())

        async def action(action_id: int) -> PositionAction:
            row = await session.get(PositionAction, action_id)
            assert row is not None
            await session.refresh(row)
            return row

        async def callbacks(user_id: int = uid) -> list[tuple[str, str | None, int | None]]:
            rows = await session.scalars(
                select(ExecutionCallback).where(ExecutionCallback.user_id == user_id)
                .order_by(ExecutionCallback.id)
            )
            return [(r.action, r.raw_data, r.chat_id) for r in rows]

        async def count_actions(user_id: int = uid) -> int:
            return int(await session.scalar(
                select(func.count()).select_from(PositionAction)
                .where(PositionAction.user_id == user_id)
            ) or 0)

        def tick() -> None:
            clock["t"] += 1

        async with client_for(state) as client:
            yield SimpleNamespace(
                tid=tid, uid=uid, other_tid=other_tid, other_uid=other_uid, state=state,
                exchange=exchange, factory=factory, sent=sent, client=client, session=session,
                chat_service=chat_service, action=action, callbacks=callbacks,
                count_actions=count_actions, tick=tick, h=auth(tid), redis=redis,
            )
        # Задачи «Да» (и случайно попавшие не в свой словарь) — с пределом: зависшая
        # задача роняет тест, а не вешает прогон.
        tasks = [*state.action_tasks.values(), *state.confirm_tasks.values()]
        if tasks:
            _, hung = await asyncio.wait(tasks, timeout=30)
            for task in hung:
                task.cancel()
            assert not hung, "задача «Да» не завершилась"
        await session.rollback()
        for user_id in (uid, other_uid):
            fresh = await UserRepository(session).get_by_id(user_id)
            if fresh is not None:
                await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


def _writes(c: Any) -> list[str]:
    return [n for n in c.exchange.names() if n in WRITES]


async def _card(c: Any, body: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await c.client.post("/api/actions", json=body or BE, headers=c.h)
    assert r.status_code == 200, r.text
    c.tick()
    data: dict[str, Any] = r.json()
    return data


async def _confirm(c: Any, action_id: int, risk: bool = False) -> dict[str, Any]:
    r = await c.client.post(f"/api/actions/{action_id}/confirm",
                            json={"risk_confirmed": risk}, headers=c.h)
    assert r.status_code == 200, r.text
    c.tick()
    data: dict[str, Any] = r.json()
    return data


# --- выключатель ----------------------------------------------------------------------


async def test_switch_off_is_403_without_exchange_or_rows(ctx) -> None:  # type: ignore[no-untyped-def]
    """MINIAPP_ACTIONS_ENABLED=false: каждый эндпоинт — 403, 0 обращений к бирже,
    ни строк действий, ни журнала нажатий."""
    ctx.state.settings = ctx.state.settings.model_copy(update={"miniapp_actions_enabled": False})
    requests = [
        ("GET", "/api/positions/XRP-USDT/LONG/actions", None),
        ("POST", "/api/actions/preview", {**BE, "kind": "sl", "level": "1.5"}),
        ("POST", "/api/actions", BE),
        ("POST", "/api/actions/1/confirm", {"risk_confirmed": False}),
        ("POST", "/api/actions/1/decline", None),
        ("GET", "/api/actions/1", None),
    ]
    for method, url, body in requests:
        r = await ctx.client.request(method, url, json=body, headers=ctx.h)
        assert r.status_code == 403, (url, r.text)
        assert r.json()["code"] == "ACTIONS_DISABLED"
    assert ctx.factory.calls == 0 and ctx.exchange.calls == []
    assert await ctx.count_actions() == 0 and await ctx.callbacks() == []


async def test_me_reports_actions_flags(ctx) -> None:  # type: ignore[no-untyped-def]
    body = (await ctx.client.get("/api/me", headers=ctx.h)).json()
    assert body["actions"] == {"enabled": True, "dry_run": False, "card_ttl_seconds": 60}


def test_switch_defaults_off() -> None:
    assert Settings(bot_token=TOKEN).miniapp_actions_enabled is False  # type: ignore[call-arg]


# --- экран действий ---------------------------------------------------------------------


async def test_actions_screen_plans_every_kind_without_rows(ctx) -> None:  # type: ignore[no-untyped-def]
    r = await ctx.client.get("/api/positions/XRP-USDT/LONG/actions", headers=ctx.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refusal"] is None
    assert body["position"]["symbol"] == "XRP-USDT" and body["position"]["side"] == "LONG"
    assert body["position"]["stops"][0]["price"] == "1.4795"
    expected_be = breakeven_price(TradeSide.LONG, ENTRY, ctx.state.settings.exec_taker_fee_rate,
                                  ENTRY * ctx.state.settings.exec_taker_fee_rate, 4)
    assert body["breakeven_price"] == str(expected_be)
    assert [(a["code"], a["enabled"]) for a in body["actions"]] == [
        ("be", True), ("sl", True), ("tp", True), ("c25", True), ("c50", True), ("cf", True),
    ]
    assert body["pnl_r_text"] is None          # сделки в журнале нет — R не знаем
    assert await ctx.count_actions() == 0 and _writes(ctx) == []


async def test_actions_screen_hints_are_core_refusals(ctx) -> None:  # type: ignore[no-untyped-def]
    """Два стопа — БУ и перенос стопа недоступны с текстом ядра; тейк и закрытия — да."""
    ctx.exchange.orders.append(_open_order("2105910661355299999", "1.47", close_position=False))
    body = (await ctx.client.get("/api/positions/XRP-USDT/LONG/actions", headers=ctx.h)).json()
    hints = {a["code"]: a for a in body["actions"]}
    assert not hints["be"]["enabled"] and "несколько стопов" in hints["be"]["hint"]
    assert not hints["sl"]["enabled"] and "несколько стопов" in hints["sl"]["hint"]
    assert hints["tp"]["enabled"] and hints["cf"]["enabled"]



async def test_actions_screen_breakeven_hint_is_threshold(ctx) -> None:  # type: ignore[no-untyped-def]
    """К4 (28.10.2026): безубыток не достигнут — под кнопкой короткая подсказка с порогом mark
    (hint ядра), а не полный текст отказа; карточка «Да» отдаёт полный текст."""
    ctx.exchange.mark = D("1.5265")
    body = (await ctx.client.get("/api/positions/XRP-USDT/LONG/actions", headers=ctx.h)).json()
    be = next(a for a in body["actions"] if a["code"] == "be")
    settings = ctx.state.settings
    level = breakeven_price(TradeSide.LONG, ENTRY, settings.exec_taker_fee_rate,
                            ENTRY * settings.exec_taker_fee_rate, 4)
    x = breakeven_threshold(TradeSide.LONG, level, settings.exec_min_stop_distance_percent, 4)
    assert be["enabled"] is False and be["hint"] == f"доступно, когда mark выше {x}"
    data = await _card(ctx)
    assert data["card"]["refusal"]["message"].startswith(
        f"Стоп в безубыток доступен, когда mark выше {x} (сейчас 1.5265, безубыток {level})"
    )


async def test_actions_screen_position_gone(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.position = None
    body = (await ctx.client.get("/api/positions/XRP-USDT/LONG/actions", headers=ctx.h)).json()
    assert body["position"] is None and body["refusal"]["code"] == "POSITION_GONE"
    assert all(not a["enabled"] for a in body["actions"])


async def test_actions_screen_on_process_cache(ctx) -> None:  # type: ignore[no-untyped-def]
    for _ in range(2):
        r = await ctx.client.get("/api/positions/XRP-USDT/LONG/actions", headers=ctx.h)
        assert r.status_code == 200
    assert ctx.exchange.names().count("positions") == 1


@pytest.mark.parametrize(("initial", "expected"), [
    ("1.53", "R не определён"),     # К3: начальный стоп в прибыли — не |вход − стоп|
    ("1.5253", "R не определён"),   # безубыток — без деления на ноль
    ("1.4795", "+0.73R"),           # стоп в убытке: PnL 1 / (0.0458 × 30) — R считается
])
async def test_actions_screen_r_from_journal_initial_stop(ctx, initial, expected) -> None:  # type: ignore[no-untyped-def]
    from app.database.repositories.trade import TradeRepository
    from app.trading.enums import TradeSource
    from app.trading.journal import TradeJournal
    from tests.test_position_action_service import PID

    trade = await TradeJournal(TradeRepository(ctx.session)).open_trade(
        user_id=ctx.uid, symbol="XRP-USDT", side=TradeSide.LONG, entry_price=ENTRY,
        quantity=D(30), stop_loss=D("1.4795"), source=TradeSource.IMPORTED,
        account_mode=ExchangeKeyMode.DEMO, external_position_id=PID, external_fill_id="f1",
    )
    trade.initial_stop_loss = D(initial)
    await ctx.session.commit()
    body = (await ctx.client.get("/api/positions/XRP-USDT/LONG/actions", headers=ctx.h)).json()
    assert body["pnl_r_text"] == expected


def test_pnl_r_text_sign_is_result() -> None:
    from tests.test_position_actions import _inputs, _position

    def text(pnl: str | None) -> str | None:
        return actions_api.pnl_r_text(_inputs(position=replace(
            _position(), unrealized_pnl=D(pnl) if pnl is not None else None)))

    one_r = _inputs().one_r_per_unit
    assert one_r is not None
    per_r = one_r * _position().quantity
    assert text(str(per_r / 2)) == "+0.50R"
    assert text(str(-per_r)) == "-1.00R"
    assert text("0.0001") == "0.00R"
    assert text(None) is None
    assert actions_api.pnl_r_text(_inputs(one_r_per_unit=None)) is None


# --- превью -------------------------------------------------------------------------------


async def test_preview_writes_nothing_and_rows_match_chat_card(ctx) -> None:  # type: ignore[no-untyped-def]
    body = {**BE, "kind": "sl", "level_percent": "1"}
    r = await ctx.client.post("/api/actions/preview", json=body, headers=ctx.h)
    assert r.status_code == 200, r.text
    card = r.json()
    # 1% ниже mark 1.56 у лонга → 1.5444, к шагу — ближе к цене.
    assert card["new_level"] == "1.5444" and card["refusal"] is None
    assert card["title"] == "XRP-USDT LONG · перенос стопа"
    plain = actions_api.plain_text(card["text"])
    for row in card["rows"]:
        assert row["value"] in plain, row
    assert await ctx.count_actions() == 0 and _writes(ctx) == []


async def test_preview_level_percent_short_is_above_mark(ctx) -> None:  # type: ignore[no-untyped-def]
    """Чип у шорта — выше mark: 1% от 1.56 → 1.5756 (к шагу — ближе к цене)."""
    assert ctx.exchange.position is not None
    ctx.exchange.position = replace(ctx.exchange.position, side=TradeSide.SHORT,
                                    entry_price=D("1.58"))
    ctx.exchange.orders = []
    body = {**BE, "side": "SHORT", "kind": "sl", "level_percent": "1"}
    card = (await ctx.client.post("/api/actions/preview", json=body, headers=ctx.h)).json()
    assert card["refusal"] is None and card["new_level"] == "1.5756"
    assert card["title"] == "XRP-USDT SHORT · перенос стопа"


async def test_preview_on_process_cache(ctx) -> None:  # type: ignore[no-untyped-def]
    for level in ("1.5", "1.51"):
        r = await ctx.client.post("/api/actions/preview",
                                  json={**BE, "kind": "sl", "level": level}, headers=ctx.h)
        assert r.status_code == 200
    assert ctx.exchange.names().count("positions") == 1


async def test_preview_refusal_has_reason(ctx) -> None:  # type: ignore[no-untyped-def]
    r = await ctx.client.post("/api/actions/preview",
                              json={**BE, "kind": "sl", "level": "1.6"}, headers=ctx.h)
    card = r.json()
    assert card["refusal"]["code"] == "STOP_WRONG_SIDE" and card["rows"] == []
    assert card["text"].startswith("⛔")


async def test_preview_exchange_down_is_503(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.exchanges.base import ExchangeUnavailableError

    async def down(*a: Any, **kw: Any) -> Any:
        raise ExchangeUnavailableError("timeout")

    monkeypatch.setattr(ctx.exchange, "get_positions", down)
    r = await ctx.client.post("/api/actions/preview",
                              json={**BE, "kind": "sl", "level": "1.5"}, headers=ctx.h)
    assert r.status_code == 503 and r.json()["code"] == "EXCHANGE_UNAVAILABLE"


@pytest.mark.parametrize("body", [
    {**C25, "level": "1.5"},                              # цена у закрытия
    {**BE, "level": "1.5"},                               # цена у безубытка
    {**BE, "kind": "sl"},                                 # стоп без цены
    {**BE, "kind": "sl", "level": "1.5", "level_percent": "1"},
    {**BE, "kind": "tp", "level_percent": "1"},           # у тейка отступа нет
    {**BE, "kind": "sl", "level": 1.5},                   # число, не строка
    {**BE, "kind": "sl", "level": "1" * 33},
    {**BE, "kind": "xx"},
    {**BE, "side": "BOTH"},
    {**BE, "symbol": "xrp-usdt"},
    {**BE, "user_id": 1},
])
async def test_bad_input_is_422(ctx, body) -> None:  # type: ignore[no-untyped-def]
    for url in ("/api/actions/preview", "/api/actions"):
        r = await ctx.client.post(url, json=body, headers=ctx.h)
        assert r.status_code == 422, (url, body, r.text)
    assert ctx.factory.calls == 0


# --- карточка ------------------------------------------------------------------------------


async def test_card_row_timer_and_audit(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)
    assert data["status"] == "CARD" and data["ttl_seconds"] == 60
    row = await ctx.action(data["action_id"])
    assert row.status is PositionActionStatus.CARD and row.card_message_id is None
    assert datetime.fromisoformat(data["expires_at"]) == row.created_at + timedelta(seconds=60)
    assert data["card"]["title"] == "XRP-USDT LONG · стоп в безубыток"
    assert await ctx.callbacks() == [("pm_open", "ma:pa:be:XRP-USDT:L", None)]
    assert _writes(ctx) == []


async def test_card_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.mark = D("1.5265")   # безубыток ещё не достигнут
    data = await _card(ctx)
    assert data["action_id"] is None and data["status"] == "REFUSED"
    assert data["card"]["refusal"]["code"] == "BREAKEVEN_NOT_REACHED"
    row = await ctx.session.scalar(select(PositionAction).where(PositionAction.user_id == ctx.uid))
    assert row is not None and row.status is PositionActionStatus.REFUSED


async def test_card_level_percent_stores_price(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx, {**BE, "kind": "sl", "level_percent": "1"})
    row = await ctx.action(data["action_id"])
    assert row.params == {"level": "1.5444", "level_percent": "1"}


async def test_card_rate_limit(ctx) -> None:  # type: ignore[no-untyped-def]
    assert (await ctx.client.post("/api/actions", json=BE, headers=ctx.h)).status_code == 200
    r = await ctx.client.post("/api/actions", json=BE, headers=ctx.h)
    assert r.status_code == 429 and r.json()["code"] == "TOO_MANY_REQUESTS"
    for i in range(6):
        r = await ctx.client.post("/api/actions/preview",
                                  json={**BE, "kind": "sl", "level": "1.5"}, headers=ctx.h)
        assert r.status_code == (429 if i == 5 else 200)


# --- «Да» ------------------------------------------------------------------------------------


async def test_confirm_close_fee_below_cent_html_in_chat_plain_in_app(ctx) -> None:  # type: ignore[no-untyped-def]
    """К4 (28.10.2026): комиссия меньше цента — в чат (HTML) «&lt;0.01», иначе Telegram
    отклонит сообщение; в Mini App (textContent) — «<0.01», без сущностей."""
    ctx.exchange.close_fee = D("0.0042")
    data = await _card(ctx, {"symbol": "XRP-USDT", "side": "LONG", "kind": "cf"})
    result = await _confirm(ctx, data["action_id"])
    assert result["final"] and "комиссия <0.01 USDT." in result["text"]
    [msg] = ctx.sent
    assert "комиссия &lt;0.01 USDT." in msg["text"]
    status = (await ctx.client.get(f"/api/actions/{data['action_id']}", headers=ctx.h)).json()
    assert "комиссия <0.01 USDT." in status["text"]


async def test_confirm_done_result_to_chat_once(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)
    action_id = data["action_id"]
    result = await _confirm(ctx, action_id)
    assert result["final"] and not result["pending"] and not result["busy"]
    assert result["text"].startswith("✅ Стоп") and result["action"]["status"] == "DONE"
    # Перенос со старым стопом — четыре шага: мост, отмена старого, closePosition, отмена моста.
    assert _writes(ctx) == ["place_conditional", "cancel", "place_conditional", "cancel"]
    [msg] = ctx.sent
    assert msg["chat_id"] == ctx.tid and msg["text"] == result["text"]
    assert (msg["meta"].kind, msg["meta"].position_action_id) == ("ACTION_RESULT", action_id)
    assert "К позициям" in str(msg["markup"])
    # Повтор — карточка уже обработана: ни второго POST, ни второго сообщения в чат.
    writes = len(_writes(ctx))
    again = await _confirm(ctx, action_id)
    assert "уже обработана" in again["text"] and len(_writes(ctx)) == writes
    assert len(ctx.sent) == 1
    assert [a for a, _, _ in await ctx.callbacks()] == ["pm_open", "pm_yes", "pm_yes"]
    assert (await ctx.callbacks())[1] == ("pm_yes", f"ma:pm:y:{action_id}", None)
    status = (await ctx.client.get(f"/api/actions/{action_id}", headers=ctx.h)).json()
    assert status["status"] == "DONE" and status["final"] and status["code"] == "be"
    assert status["text"] == actions_api.plain_text(result["text"])


async def test_confirm_dry_run_goes_to_chat(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.state.settings = ctx.state.settings.model_copy(update={"exec_dry_run": True})
    data = await _card(ctx, C25)
    result = await _confirm(ctx, data["action_id"])
    assert result["action"]["status"] == "DRY_RUN" and _writes(ctx) == []
    assert len(ctx.sent) == 1 and "Сухой прогон" in ctx.sent[0]["text"]


async def test_confirm_invalidates_positions_cache(ctx) -> None:  # type: ignore[no-untyped-def]
    key = positions_api.cache_key(ctx.state, ctx.uid)
    await ctx.state.market_cache.get_or_fetch(key, 60, _stale)
    data = await _card(ctx, C25)
    await _confirm(ctx, data["action_id"])
    fetched = await ctx.state.market_cache.get_or_fetch(key, 60, _fresh)
    assert fetched == "fresh"


async def _stale() -> str:
    return "stale"


async def _fresh() -> str:
    return "fresh"


async def test_risk_increase_needs_flag(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx, {**BE, "kind": "sl", "level": "1.45"})
    assert data["card"]["risk_increase"] is True
    refused = await _confirm(ctx, data["action_id"], risk=False)
    assert refused["action"]["status"] == "REFUSED"
    assert refused["action"]["error_code"] == "RISK_INCREASE_NOT_CONFIRMED"
    assert _writes(ctx) == [] and ctx.sent == []
    data = await _card(ctx, {**BE, "kind": "sl", "level": "1.45"})
    done = await _confirm(ctx, data["action_id"], risk=True)
    assert done["action"]["status"] == "DONE"
    assert (await ctx.callbacks())[-1] == ("pm_yes_risk", f"ma:pm:r:{data['action_id']}", None)


async def test_decline(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)
    r = await ctx.client.post(f"/api/actions/{data['action_id']}/decline", headers=ctx.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["final"] and body["action"]["status"] == "DECLINED"
    assert body["text"] == "Отменено — на биржу ничего не отправлено."
    assert (await ctx.callbacks())[-1] == ("pm_no", f"ma:pm:n:{data['action_id']}", None)
    assert _writes(ctx) == [] and ctx.sent == []


async def test_audit_failure_is_503_nothing_sent(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)

    async def broken(*a: Any, **kw: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(actions_api, "record_callback", broken)
    r = await ctx.client.post(f"/api/actions/{data['action_id']}/confirm",
                              json={"risk_confirmed": False}, headers=ctx.h)
    assert r.status_code == 503 and r.json()["code"] == "AUDIT_FAILED"
    assert _writes(ctx) == [] and ctx.sent == [] and ctx.state.action_tasks == {}
    assert (await ctx.action(data["action_id"])).status is PositionActionStatus.CARD


async def test_lock_busy_is_busy(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)
    key = position_lock_key(ctx.uid, "XRP-USDT", "LONG")
    async with RedisLock(ctx.redis, key, 30):
        result = await _confirm(ctx, data["action_id"])
    assert result["busy"] and not result["final"] and not result["pending"]
    assert result["action"]["status"] == "CARD" and _writes(ctx) == [] and ctx.sent == []


async def test_expired_card_not_to_chat(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)
    row = await ctx.action(data["action_id"])
    row.created_at = row.created_at - timedelta(seconds=61)
    await ctx.session.commit()
    result = await _confirm(ctx, data["action_id"])
    assert result["action"]["status"] == "EXPIRED" and _writes(ctx) == [] and ctx.sent == []


async def test_one_task_per_action_and_own_dict(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Двойное нажатие — одна задача; словарь — свой, не общий с открытиями."""
    data = await _card(ctx)
    action_id = data["action_id"]
    gate = asyncio.Event()
    calls: list[int] = []
    real = actions_api._confirm

    async def spy(state: ApiState, user_id: int, aid: int, risk: bool) -> Any:
        calls.append(aid)
        await gate.wait()
        return await real(state, user_id, aid, risk)

    monkeypatch.setattr(actions_api, "_confirm", spy)
    try:
        first = await actions_api.confirm(ctx.state, ctx.uid, action_id, False, wait_seconds=0.05)
        second = await actions_api.confirm(ctx.state, ctx.uid, action_id, False,
                                           wait_seconds=0.05)
        assert first is None and second is None and calls == [action_id]
        assert action_id in ctx.state.action_tasks and action_id not in ctx.state.confirm_tasks
    finally:
        gate.set()   # упавшая проверка не должна оставить задачу ждать вечно
    await ctx.state.action_tasks[action_id]
    assert len(ctx.sent) == 1 and action_id not in ctx.state.action_tasks


async def test_pending_answer_has_status(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)
    gate = asyncio.Event()
    real = actions_api.confirm

    async def short(state: ApiState, user_id: int, aid: int, risk: bool) -> Any:
        return await real(state, user_id, aid, risk, wait_seconds=0.05)

    async def held(state: ApiState, user_id: int, aid: int, risk: bool) -> Any:
        await gate.wait()

    monkeypatch.setattr(actions_api, "confirm", short)
    monkeypatch.setattr(actions_api, "_confirm", held)
    try:
        result = await _confirm(ctx, data["action_id"])
        assert result["pending"] and not result["final"]
        assert result["action"]["status"] == "CARD" and result["text"] == result["action"]["text"]
    finally:
        gate.set()


async def test_shutdown_drains_action_tasks(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    from app.api.main import drain

    gate = asyncio.Event()

    async def slow() -> None:
        await gate.wait()

    ctx.state.action_tasks[1] = asyncio.create_task(slow())
    await drain(ctx.state, timeout=0.05)
    assert any("доведёт цикл бота" in r.getMessage() for r in caplog.records)
    gate.set()
    await drain(ctx.state, timeout=1)
    assert ctx.state.action_tasks[1].done()
    del ctx.state.action_tasks[1]


# --- владение -------------------------------------------------------------------------------


async def test_other_user_action_is_404(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)
    other = auth(ctx.other_tid)
    for method, url, body in [
        ("POST", f"/api/actions/{data['action_id']}/confirm", {"risk_confirmed": False}),
        ("POST", f"/api/actions/{data['action_id']}/decline", None),
        ("GET", f"/api/actions/{data['action_id']}", None),
    ]:
        r = await ctx.client.request(method, url, json=body, headers=other)
        ctx.tick()
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND", url
    assert _writes(ctx) == [] and await ctx.callbacks(ctx.other_uid) == []
    assert (await ctx.action(data["action_id"])).status is PositionActionStatus.CARD


async def test_other_account_action_is_404(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)
    row = await ctx.action(data["action_id"])
    row.account_mode = ExchangeKeyMode.LIVE
    await ctx.session.commit()
    r = await ctx.client.post(f"/api/actions/{data['action_id']}/confirm",
                              json={"risk_confirmed": False}, headers=ctx.h)
    assert r.status_code == 404 and _writes(ctx) == []


async def test_chat_card_via_mini_app_is_stale(ctx) -> None:  # type: ignore[no-untyped-def]
    """Карточка чата (card_message_id есть) через Mini App — «Это не последняя
    карточка», 0 POST, в чат ничего."""
    outcome = await (await ctx.chat_service()).open_card(
        PositionActionKind.CLOSE_FULL, {}, "XRP-USDT", TradeSide.LONG, message_id=55
    )
    assert outcome.action is not None
    result = await _confirm(ctx, outcome.action.id)
    assert "не последняя карточка" in result["text"]
    assert result["action"]["status"] == "CARD" and _writes(ctx) == [] and ctx.sent == []


# --- две карточки одной позиции: чат и Mini App (решение владельца 25.10, п.4) ----------------


async def test_mini_app_close_then_chat_stop_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    chat = await (await ctx.chat_service()).open_card(
        PositionActionKind.MOVE_STOP, {"level": "1.5"}, "XRP-USDT", TradeSide.LONG, message_id=55
    )
    assert chat.action is not None
    mini = await _card(ctx, C25)
    assert (await _confirm(ctx, mini["action_id"]))["action"]["status"] == "DONE"
    writes = len(_writes(ctx))
    result = await (await ctx.chat_service()).confirm(chat.action.id, message_id=55,
                                                      risk_confirmed=False)
    row = await ctx.action(chat.action.id)
    assert row.status is PositionActionStatus.REFUSED and row.error_code == "POSITION_CHANGED"
    assert "Объём позиции изменился: 30 → 23" in result.text
    assert len(_writes(ctx)) == writes


async def test_chat_stop_then_mini_app_close_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    mini = await _card(ctx, C25)
    chat = await (await ctx.chat_service()).open_card(
        PositionActionKind.MOVE_STOP, {"level": "1.5"}, "XRP-USDT", TradeSide.LONG, message_id=55
    )
    assert chat.action is not None
    done = await (await ctx.chat_service()).confirm(chat.action.id, message_id=55,
                                                    risk_confirmed=False)
    assert done.text.startswith("✅ Стоп")
    writes = len(_writes(ctx))
    result = await _confirm(ctx, mini["action_id"])
    assert result["action"]["status"] == "REFUSED"
    assert result["action"]["error_code"] == "POSITION_CHANGED"
    assert "Стоп или тейк на бирже изменились" in result["text"]
    assert len(_writes(ctx)) == writes and ctx.sent == []


# --- текст итога -----------------------------------------------------------------------------


def test_plain_text_strips_tags_and_entities() -> None:
    assert actions_api.plain_text("<b>A</b> &amp; &lt;x&gt; <i>y</i>") == "A & <x> y"


async def test_status_text_from_outbox_is_plain(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx)
    await _confirm(ctx, data["action_id"])
    meta = OutgoingMeta(user_id=ctx.uid, kind="ACTION_RECOVERED",
                        position_action_id=data["action_id"])
    await outbox.record(ctx.state.db, meta, chat_id=ctx.tid, message_id=999,
                        text="<b>Итог</b>: стоп &amp; тейк &lt;сняты&gt;")
    status = (await ctx.client.get(f"/api/actions/{data['action_id']}", headers=ctx.h)).json()
    assert status["text"] == "Итог: стоп & тейк <сняты>"


async def test_status_text_without_outbox(ctx) -> None:  # type: ignore[no-untyped-def]
    data = await _card(ctx, {**BE, "kind": "sl", "level": "1.45"})
    await _confirm(ctx, data["action_id"], risk=False)
    status = (await ctx.client.get(f"/api/actions/{data['action_id']}", headers=ctx.h)).json()
    assert status["text"] == "⛔ Риск растёт — нужна кнопка «⚠️ Да, увеличить риск»."
    assert status["final"] and status["expires_at"] is None and status["decided_at"]


# --- строки карточки = карточка чата ---------------------------------------------------------


@pytest.mark.parametrize("name", [
    "breakeven", "stop_closer", "stop_risk_up", "stop_none", "stop_short", "stop_locked",
    "take_new", "take_move", "close_25", "close_50_sized", "close_full", "close_full_no_stop",
])
def test_card_rows_are_chat_card_strings(name: str) -> None:
    from app.execution.position_action_service import CardOutcome
    from app.execution.position_actions import card_view
    from tests.test_position_actions import _inputs
    from tests.test_position_actions_golden import CASES

    kind, params, overrides = CASES[name]
    inputs = _inputs(**overrides)
    plan = plan_action(kind, params, inputs)
    assert isinstance(plan, ActionPlan), plan
    text = render_card(plan, inputs)
    out = actions_api.card_out(CardOutcome(text, None, plan.risk_increase,
                                           view=card_view(plan, inputs)))
    plain = actions_api.plain_text(text)
    assert out.rows and all(row.value in plain for row in out.rows), name
    own = ("Стоп — на всю позицию", "Тейк — на всю позицию", "Комиссия выхода учтена в риске")
    assert all(note in plain or note in own for note in out.notes), name


@pytest.mark.parametrize("kind,params", [
    (PositionActionKind.MOVE_STOP, {"level": "1.5"}),
    (PositionActionKind.CLOSE_FULL, {}),
])
def test_card_rows_fee_below_cent_as_chat(kind: PositionActionKind, params: dict) -> None:
    """К4 (28.10.2026): комиссия меньше цента — в строках Mini App «<0.01 USDT» без «≈», как в
    карточке чата."""
    from app.execution.position_action_service import CardOutcome
    from app.execution.position_actions import card_view
    from tests.test_position_actions import _inputs, _position

    inputs = _inputs(position=_position(qty="1"), one_r_per_unit=None)
    plan = plan_action(kind, params, inputs)
    assert isinstance(plan, ActionPlan), plan
    text = render_card(plan, inputs)
    out = actions_api.card_out(CardOutcome(text, None, plan.risk_increase,
                                           view=card_view(plan, inputs)))
    fees = [row.value for row in out.rows if row.label.startswith("Комиссия")]
    assert fees == ["<0.01 USDT"]
    assert "<0.01 USDT" in actions_api.plain_text(text)
