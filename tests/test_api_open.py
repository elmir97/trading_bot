"""API Mini App, подэтап 1.5: preview / open / confirm / status / cancel /
?active=1 (§3, §8) — против настоящей БД, FakeRedis и фейковой биржи ядра
(tests/opening_fakes.py)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.api import opening as opening_api
from app.api import ratelimit
from app.api.deps import ApiState
from app.core.config import Settings
from app.database.models.execution_callback import ExecutionCallback
from app.database.models.trade_opening import TradeOpening
from app.database.models.user import User
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.execution.opening.calc import OpeningInputs
from app.execution.opening.service import OpeningService
from app.market.cache import TTLCache
from app.services.user_service import UserService
from app.trading.enums import (
    EntryType,
    ExchangeKeyMode,
    OpeningSource,
    OpeningStatus,
    TradeSide,
)
from tests.api_helpers import TOKEN, auth, client_for
from tests.conftest import cleanup_user
from tests.opening_fakes import OpeningExchange

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)
# То же, что INPUTS чат-мастера в test_opening_confirm — ценой и %.
BODY: dict[str, Any] = {
    "symbol": "XRP-USDT", "side": "LONG", "entry_type": "MARKET",
    "stop": {"kind": "price", "value": "1.4501"}, "take": {"kind": "price", "value": "1.5399"},
    "risk": {"kind": "percent", "value": "1"}, "leverage": 10,
}
WIZARD = OpeningInputs(
    symbol="XRP-USDT", side=TradeSide.LONG, entry_type=EntryType.MARKET,
    stop_loss=D("1.4501"), take_profit=D("1.5399"), risk_percent=D(1), leverage=10,
)
LIMIT_BODY = {**BODY, "entry_type": "LIMIT", "limit_price": "1.4200",
              "stop": {"kind": "price", "value": "1.3900"}, "take": None,
              "expiry_minutes": 60}


class Factory:
    def __init__(self, exchange: OpeningExchange) -> None:
        self.exchange = exchange

    async def get_credentials(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
        return SimpleNamespace(is_read_only=False, permissions_checked_at=NOW, user_id=user_id,
                               mode=mode)

    async def for_user(self, session, user_id, exchange_name="bingx", mode=None):  # type: ignore[no-untyped-def]
        return self.exchange

    def public_client(self) -> OpeningExchange:
        return self.exchange


class FakeBot:
    def __init__(self) -> None:
        self.stripped: list[tuple[int, int]] = []

    async def edit_message_reply_markup(self, *, chat_id: int, message_id: int,
                                        reply_markup: Any = None) -> None:
        self.stripped.append((chat_id, message_id))


async def _make_user(db: Database, settings: Settings, telegram_id: int) -> int:
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=telegram_id)
        user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
        plan = await UserRepository(session).get_trading_plan(user.id)
        assert plan is not None
        plan.risk_per_trade_percent = D(2)
        plan.max_leverage = 20
        plan.min_risk_reward = D("0.5")
        await session.commit()
        return user.id


@pytest_asyncio.fixture
async def ctx(unique_telegram_id, monkeypatch):  # type: ignore[no-untyped-def]
    tid, other_tid = unique_telegram_id(), unique_telegram_id()
    settings = Settings(  # type: ignore[call-arg]
        bot_token=TOKEN, trading_execution_enabled=True, bingx_trading_mode="demo",
        exec_dry_run=False, exec_open_dry_run=False, exec_order_readback_delay_ms=0,
        miniapp_allowed_telegram_ids=f"{tid},{other_tid}",
    )
    db = Database(settings)
    uid = await _make_user(db, settings, tid)
    other_uid = await _make_user(db, settings, other_tid)
    exchange = OpeningExchange()
    bot = FakeBot()
    retries: list[int] = []
    state = ApiState(settings=settings, db=db, redis=FakeRedis(decode_responses=True),
                     factory=Factory(exchange), bot=bot, alarm_retry=retries.append)
    sent: list[dict[str, Any]] = []

    async def fake_send(bot: Any, db: Any, telegram_id: int, text: str, **kw: Any) -> None:
        sent.append({"telegram_id": telegram_id, "text": text, **kw})

    monkeypatch.setattr(opening_api, "send_opening_result", fake_send)
    # Окно лимита частоты — одна «секунда» на тест, пока его не сдвинут.
    clock = {"t": 1_800_000_000}
    monkeypatch.setattr(ratelimit, "window", lambda: clock["t"])

    async with db.session() as session:

        async def service(user_id: int = uid) -> OpeningService:
            user = await session.scalar(
                select(User).where(User.id == user_id).options(selectinload(User.settings))
            )
            assert user is not None
            return OpeningService(session, settings, None, user, redis=state.redis,
                                  factory=Factory(exchange), market_cache=TTLCache())

        async def count(model: Any, user_id: int = uid) -> int:
            return int(await session.scalar(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            ) or 0)

        def tick() -> None:
            clock["t"] += 1

        async with client_for(state) as client:
            yield SimpleNamespace(
                tid=tid, uid=uid, other_tid=other_tid, other_uid=other_uid, state=state,
                exchange=exchange, bot=bot, sent=sent, retries=retries, client=client,
                session=session, service=service, count=count, tick=tick, h=auth(tid),
            )
        for task in list(state.confirm_tasks.values()):
            await task
        await session.rollback()
        for user_id in (uid, other_uid):
            fresh = await UserRepository(session).get_by_id(user_id)
            if fresh is not None:
                await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


def _market_posts(c: Any) -> list[str]:
    return [n for n, _ in c.exchange.posts() if n in ("post_market", "post_limit")]


async def _open(c: Any, body: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await c.client.post("/api/open", json=body or BODY, headers=c.h)
    assert r.status_code == 200, r.text
    c.tick()
    data: dict[str, Any] = r.json()
    return data


async def _row(c: Any, opening_id: int) -> TradeOpening:
    row = await c.session.get(TradeOpening, opening_id)
    assert row is not None
    await c.session.refresh(row)
    return row


# --- preview ------------------------------------------------------------------------------


@pytest.mark.parametrize(("exchange_max", "leverage", "code"), [
    (15, 16, "LEVERAGE_ABOVE_EXCHANGE"),        # максимум контракта 15, план 20
    (125, 21, "PLAN_LEVERAGE_TOO_HIGH"),        # максимум плана 20
])
async def test_leverage_above_max_is_refused_by_server(ctx, exchange_max, leverage, code) -> None:  # type: ignore[no-untyped-def]
    """Пункт 3: плечо выше min(максимум контракта, план) отклоняет сервер — не только
    форма: превью и карточка BLOCK, «Да» на бирже ничего не открывает."""
    ctx.exchange.max_leverage = exchange_max
    body = {**BODY, "leverage": leverage}
    preview = (await ctx.client.post("/api/open/preview", json=body, headers=ctx.h)).json()
    assert preview["can_open"] is False
    assert (code, "BLOCK") in {(i["code"], i["level"]) for i in preview["issues"]}
    card = await _open(ctx, body)
    assert card["card"]["can_open"] is False
    oid = card["opening_id"]
    if oid is not None:
        r = await ctx.client.post(f"/api/open/{oid}/confirm", json={"accept_warnings": True},
                                  headers=ctx.h)
        assert r.json()["opening"]["status"] != "DONE"
    assert _market_posts(ctx) == []
    # Ровно на максимуме — можно.
    ctx.tick()
    edge = {**BODY, "leverage": min(exchange_max, 20)}
    ok = (await ctx.client.post("/api/open/preview", json=edge, headers=ctx.h)).json()
    assert code not in {i["code"] for i in ok["issues"]}


async def test_preview_writes_nothing_and_equals_wizard_card(ctx) -> None:  # type: ignore[no-untyped-def]
    r = await ctx.client.post("/api/open/preview", json=BODY, headers=ctx.h)
    assert r.status_code == 200, r.text
    card = r.json()
    assert await ctx.count(TradeOpening) == 0 and await ctx.count(ExecutionCallback) == 0
    assert ctx.exchange.posts() == []
    # Одно ядро: превью Mini App = карточка чат-мастера на тех же данных.
    wizard = await (await ctx.service()).prepare(WIZARD, source=OpeningSource.WIZARD)
    assert wizard.opening is not None and wizard.calc is not None
    assert card["text"] == wizard.text
    assert card["calc"]["quantity"] == str(wizard.calc.quantity)
    assert card["calc"]["risk_usd"] == str(wizard.calc.risk_usd.quantize(D("0.01")))
    assert card["levels"]["stop_loss"] == "1.4501" and card["can_open"] is True


@pytest.mark.parametrize(("side", "stop", "take_2r"), [
    ("LONG", "1.4501", "1.5848"),       # 1.4950 + 2·0.0449
    ("SHORT", "1.5399", "1.4052"),      # 1.4950 − 2·0.0449
])
async def test_preview_take_2r_from_server(ctx, side, stop, take_2r) -> None:  # type: ignore[no-untyped-def]
    """Чип «2R» формы подставляет цену сервера: тейк на 2R от входа и стопа превью;
    с этим тейком RR = 2.00, «округлено» нет. Без стопа с нужной стороны — null."""
    body = {**BODY, "side": side, "stop": {"kind": "price", "value": stop}, "take": None}
    card = (await ctx.client.post("/api/open/preview", json=body, headers=ctx.h)).json()
    assert card["calc"]["take_2r"] == take_2r
    ctx.tick()
    body["take"] = {"kind": "price", "value": take_2r}
    card = (await ctx.client.post("/api/open/preview", json=body, headers=ctx.h)).json()
    assert card["calc"]["rr"] == "2.00" and card["rounded"] == []
    assert card["levels"]["take_profit"] == take_2r
    ctx.tick()
    wrong = {**body, "stop": {"kind": "price", "value": take_2r}}       # стоп по ту сторону
    card = (await ctx.client.post("/api/open/preview", json=wrong, headers=ctx.h)).json()
    assert card["calc"]["take_2r"] is None


async def test_preview_decimals_are_strings(ctx) -> None:  # type: ignore[no-untyped-def]
    body = {**BODY, "stop": {"kind": "percent", "value": "3"},
            "risk": {"kind": "usd", "value": "15"}}
    card = (await ctx.client.post("/api/open/preview", json=body, headers=ctx.h)).json()
    calc, levels = card["calc"], card["levels"]
    for value in (calc["quantity"], calc["risk_usd"], calc["margin"], calc["fee_usd"],
                  calc["entry_price"], levels["stop_loss"], levels["risk_percent"]):
        assert isinstance(value, str), value
    # 3 % от last 1.4950 = 1.450150 → шаг 0.0001 вниз (LONG); $15 от equity 1500 = 1 %.
    assert levels["stop_loss"] == "1.4501" and levels["risk_percent"] == "1.0000"
    assert calc["risk_usd"].count(".") == 1 and len(calc["risk_usd"].split(".")[1]) == 2
    assert card["rounded"] == []                       # стоп в % — «округлено» не показываем


async def test_preview_reads_through_cache(ctx) -> None:  # type: ignore[no-untyped-def]
    for _ in range(3):
        r = await ctx.client.post("/api/open/preview", json=BODY, headers=ctx.h)
        assert r.status_code == 200
    names = ctx.exchange.names()
    # marginType — 2/с у BingX: три превью подряд — один запрос.
    assert names.count("margin_type") == 1 and names.count("balance") == 1
    assert names.count("positions") == 1 and names.count("leverage") == 1


async def test_preview_client_cannot_write(ctx) -> None:  # type: ignore[no-untyped-def]
    reads = opening_api.PreviewReads(ctx.exchange, ctx.state, ctx.uid)
    with pytest.raises(AttributeError, match="не пишет"):
        reads.place_market_order  # noqa: B018
    with pytest.raises(AttributeError):
        reads.set_leverage  # noqa: B018


@pytest.mark.parametrize("patch", [
    {"entry_type": "LIMIT"},                                   # лимит без цены
    {"entry_type": "LIMIT", "limit_price": "1.42", "expiry_minutes": 30},
    {"stop": {"kind": "price", "value": "NaN"}},
    {"risk": {"kind": "percent", "value": "-1"}},
    {"symbol": "XRP-USDC"},
    {"extra": 1},
])
async def test_preview_bad_input(ctx, patch) -> None:  # type: ignore[no-untyped-def]
    r = await ctx.client.post("/api/open/preview", json={**BODY, **patch}, headers=ctx.h)
    assert (r.status_code, r.json()["code"]) == (422, "BAD_REQUEST")
    assert ctx.exchange.calls == []


# --- open + confirm -----------------------------------------------------------------------


async def test_open_and_confirm_market(ctx) -> None:  # type: ignore[no-untyped-def]
    card = await _open(ctx)
    oid = card["opening_id"]
    row = await _row(ctx, oid)
    assert card["status"] == "CARD" and row.source is OpeningSource.MINIAPP
    assert card["expires_at"] is not None
    r = await ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["final"] is True and body["pending"] is False
    view = body["opening"]
    assert view["status"] == "DONE" and view["trade_id"] and view["final"] is True
    assert view["stop_placed"] is True and view["take_placed"] is True
    assert isinstance(view["avg_price"], str) and view["avg_price"] == "1.4950"
    assert _market_posts(ctx) == ["post_market"]
    # аудит нажатия — как у кнопок чата
    rows = list(await ctx.session.scalars(
        select(ExecutionCallback).where(ExecutionCallback.user_id == ctx.uid)
    ))
    assert [(x.action, x.trade_opening_id) for x in rows] == [("ma_confirm", oid)]
    # итог — в чат тем же сообщением, что у цикла открытий
    assert len(ctx.sent) == 1 and ctx.sent[0]["telegram_id"] == ctx.tid
    assert ctx.sent[0]["status"] is OpeningStatus.DONE
    assert ctx.sent[0]["position"] == ("XRP-USDT", TradeSide.LONG)
    assert ctx.sent[0]["meta"].kind == "OPEN_DONE" and ctx.sent[0]["text"] == body["text"]
    status = (await ctx.client.get(f"/api/open/{oid}", headers=ctx.h)).json()
    assert status == view


async def test_confirm_audit_failure_sends_nothing(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    oid = (await _open(ctx))["opening_id"]

    async def broken(*a: Any, **k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(opening_api, "record_callback", broken)
    r = await ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h)
    assert (r.status_code, r.json()["code"]) == (503, "AUDIT_FAILED")
    assert _market_posts(ctx) == [] and (await _row(ctx, oid)).status is OpeningStatus.CARD


async def test_parallel_confirms_one_post(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    oid = (await _open(ctx))["opening_id"]
    runs: list[int] = []
    real = opening_api._confirm

    async def spy(*a: Any, **k: Any) -> Any:
        runs.append(1)
        return await real(*a, **k)

    monkeypatch.setattr(opening_api, "_confirm", spy)
    first, second = await asyncio.gather(
        opening_api.confirm(ctx.state, ctx.uid, oid, False),
        opening_api.confirm(ctx.state, ctx.uid, oid, False),
    )
    assert runs == [1]                                # второй ждёт ту же задачу
    assert first == second and first is not None and first.status is OpeningStatus.DONE
    assert _market_posts(ctx) == ["post_market"] and len(ctx.sent) == 1


async def test_parallel_confirm_chat_and_miniapp_one_post(ctx) -> None:  # type: ignore[no-untyped-def]
    oid = (await _open(ctx))["opening_id"]
    chat = await ctx.service()
    api_out, chat_out = await asyncio.gather(
        opening_api.confirm(ctx.state, ctx.uid, oid, False),
        chat.confirm(oid, accept_warnings=False),
    )
    assert _market_posts(ctx) == ["post_market"]
    statuses = {api_out.status if api_out else None, chat_out.status}
    assert OpeningStatus.DONE in statuses
    assert not (api_out and api_out.final and chat_out.final)   # итог показал один


async def test_double_confirm_request_rate_limited(ctx) -> None:  # type: ignore[no-untyped-def]
    oid = (await _open(ctx))["opening_id"]
    r1, r2 = await asyncio.gather(
        ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h),
        ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 429]
    limited = r1 if r1.status_code == 429 else r2
    assert limited.json()["code"] == "TOO_MANY_REQUESTS"
    assert _market_posts(ctx) == ["post_market"]
    assert await ctx.count(ExecutionCallback) == 1               # 429 — до аудита


async def test_confirm_pending_then_status(ctx) -> None:  # type: ignore[no-untyped-def]
    oid = (await _open(ctx))["opening_id"]
    gate = asyncio.Event()
    original = ctx.exchange.place_market_order

    async def slow(**kw: Any) -> Any:
        await gate.wait()
        return await original(**kw)

    ctx.exchange.place_market_order = slow
    assert await opening_api.confirm(ctx.state, ctx.uid, oid, False, wait_seconds=0.2) is None
    mid = (await ctx.client.get(f"/api/open/{oid}", headers=ctx.h)).json()
    assert mid["status"] in ("CONFIRMED", "SUBMITTING") and mid["final"] is False
    gate.set()
    await ctx.state.confirm_tasks[oid]
    done = (await ctx.client.get(f"/api/open/{oid}", headers=ctx.h)).json()
    assert done["status"] == "DONE" and oid not in ctx.state.confirm_tasks


async def test_confirm_expired_card(ctx) -> None:  # type: ignore[no-untyped-def]
    oid = (await _open(ctx))["opening_id"]
    await ctx.session.execute(update(TradeOpening).where(TradeOpening.id == oid).values(
        created_at=datetime.now(UTC) - timedelta(seconds=125)))
    await ctx.session.commit()
    body = (await ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h)).json()
    assert body["opening"]["status"] == "EXPIRED_CARD" and "устарела" in body["text"]
    assert ctx.exchange.posts() == [] and ctx.sent == []        # в чат — нет


async def test_confirm_warnings_need_accept(ctx) -> None:  # type: ignore[no-untyped-def]
    oid = (await _open(ctx, {**BODY, "take": {"kind": "price", "value": "1.5100"}}))["opening_id"]
    row = await _row(ctx, oid)
    assert row.status is OpeningStatus.CARD and row.violations    # RR ниже 1 — WARN
    no = (await ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h)).json()
    assert no["final"] is False and "всё равно" in no["text"] and ctx.exchange.posts() == []
    ctx.tick()
    yes = await ctx.client.post(f"/api/open/{oid}/confirm", json={"accept_warnings": True},
                                headers=ctx.h)
    assert yes.json()["opening"]["status"] == "DONE"
    raws = list(await ctx.session.scalars(
        select(ExecutionCallback.raw_data).where(ExecutionCallback.trade_opening_id == oid)
        .order_by(ExecutionCallback.id)
    ))
    assert raws == [f"ma:confirm:{oid}:0", f"ma:confirm:{oid}:1"]


async def test_alarm_starts_fast_retries(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.exchange.drop_attached_sl = True
    ctx.exchange.fail_conditional = True
    ctx.exchange.fail_close = True
    oid = (await _open(ctx))["opening_id"]
    body = (await ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h)).json()
    assert body["opening"]["status"] == "ALARM" and body["opening"]["final"] is False
    assert ctx.retries == [oid]
    assert ctx.sent[0]["status"] is OpeningStatus.ALARM and ctx.sent[0]["opening_id"] == oid


async def test_shutdown_drains_confirm(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """Остановка API ждёт начатое «Открыть»; не дождалась — WARNING, дальше цикл бота."""
    from app.api.main import drain

    gate = asyncio.Event()

    async def slow() -> None:
        await gate.wait()

    ctx.state.confirm_tasks[1] = asyncio.create_task(slow())
    await drain(ctx.state, timeout=0.05)
    assert any("доведёт цикл бота" in r.getMessage() for r in caplog.records)
    gate.set()
    await drain(ctx.state, timeout=1)
    assert ctx.state.confirm_tasks[1].done()
    del ctx.state.confirm_tasks[1]


# --- владение, 404 ------------------------------------------------------------------------


async def test_foreign_and_missing_id_404(ctx) -> None:  # type: ignore[no-untyped-def]
    foreign = await (await ctx.service(ctx.other_uid)).prepare(
        WIZARD, source=OpeningSource.MINIAPP
    )
    assert foreign.opening is not None
    fid = foreign.opening.id
    for oid in (fid, fid + 10_000_000):
        for method, path, json in (
            ("post", f"/api/open/{oid}/confirm", {}),
            ("get", f"/api/open/{oid}", None),
            ("post", f"/api/open/{oid}/cancel", None),
        ):
            r = await ctx.client.request(method, path, json=json, headers=ctx.h)
            assert (r.status_code, r.json()) == (
                404, {"code": "NOT_FOUND", "message": "Открытие не найдено."}
            ), path
            ctx.tick()
    assert ctx.exchange.posts() == []
    assert await ctx.count(ExecutionCallback) == 0                     # чужое не пишем
    assert (await _row(ctx, fid)).status is OpeningStatus.CARD
    # хозяин видит своё
    r = await ctx.client.get(f"/api/open/{fid}", headers=auth(ctx.other_tid))
    assert r.status_code == 200


# --- лимит: cancel, ?active=1 -----------------------------------------------------------------


async def test_limit_active_and_cancel(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    retired: list[tuple[int, Any]] = []

    async def retire(bot: Any, db: Any, opening_id: int, status: Any) -> int:
        retired.append((opening_id, status))
        return 1

    monkeypatch.setattr(opening_api.opening_messages, "retire_working", retire)
    oid = (await _open(ctx, LIMIT_BODY))["opening_id"]
    body = (await ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h)).json()
    assert body["opening"]["status"] == "WORKING" and body["opening"]["expires_at"]
    assert ctx.sent[0]["status"] is OpeningStatus.WORKING
    active = await ctx.client.get("/api/open", params={"active": "1"}, headers=ctx.h)
    assert [x["id"] for x in active.json()] == [oid]
    assert active.json()[0]["limit_price"] == "1.4200"
    assert (await ctx.client.get("/api/open", headers=ctx.h)).status_code == 422
    r = await ctx.client.post(f"/api/open/{oid}/cancel", headers=ctx.h)
    assert r.status_code == 200, r.text
    assert r.json()["opening"]["status"] == "CANCELLED" and r.json()["final"] is True
    assert [n for n, _ in ctx.exchange.posts()].count("post_cancel") == 1
    assert retired == [(oid, OpeningStatus.CANCELLED)]
    actions = list(await ctx.session.scalars(
        select(ExecutionCallback.action).where(ExecutionCallback.trade_opening_id == oid)
        .order_by(ExecutionCallback.id)
    ))
    assert actions == ["ma_confirm", "ma_cancel"]
    ctx.tick()
    gone = await ctx.client.get("/api/open", params={"active": "1"}, headers=ctx.h)
    assert gone.json() == []


@pytest.mark.parametrize("confirmed", [False, True])
async def test_cancel_not_limit_409(ctx, confirmed) -> None:  # type: ignore[no-untyped-def]
    oid = (await _open(ctx))["opening_id"]
    if confirmed:
        await ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h)
        ctx.tick()
    r = await ctx.client.post(f"/api/open/{oid}/cancel", headers=ctx.h)
    assert (r.status_code, r.json()["code"]) == (409, "NOT_WORKING")
    expected = "✅ Открыто — сделка #" if confirmed else "Карточка ещё не подтверждена"
    assert r.json()["message"].startswith(expected)
    assert "post_cancel" not in ctx.exchange.names()
    actions = list(await ctx.session.scalars(
        select(ExecutionCallback.action).where(ExecutionCallback.trade_opening_id == oid)
    ))
    assert "ma_cancel" in actions                                    # нажатие записано


# --- 429 ---------------------------------------------------------------------------------


async def test_rate_limits(ctx) -> None:  # type: ignore[no-untyped-def]
    codes = [(await ctx.client.post("/api/open/preview", json=BODY, headers=ctx.h)).status_code
             for _ in range(6)]
    assert codes == [200] * 5 + [429]
    r = await ctx.client.post("/api/open", json=BODY, headers=ctx.h)
    again = await ctx.client.post("/api/open", json=BODY, headers=ctx.h)
    assert (r.status_code, again.status_code) == (200, 429)
    assert again.json()["message"] == ratelimit.TOO_MANY_TEXT
    ctx.tick()
    assert (await ctx.client.post("/api/open", json=BODY, headers=ctx.h)).status_code == 200
    # у другого пользователя своё окно
    other = await ctx.client.post("/api/open/preview", json=BODY, headers=auth(ctx.other_tid))
    assert other.status_code == 200


# --- вытеснение карточек в обе стороны -----------------------------------------------------------


async def test_miniapp_card_supersedes_chat_card(ctx) -> None:  # type: ignore[no-untyped-def]
    chat = await (await ctx.service()).prepare(
        WIZARD, source=OpeningSource.WIZARD, chat_id=1, message_id=55
    )
    assert chat.opening is not None
    await _open(ctx)
    assert (await _row(ctx, chat.opening.id)).status is OpeningStatus.EXPIRED_CARD
    assert ctx.bot.stripped == [(1, 55)]                  # кнопки карточки в чате сняты


async def test_chat_card_supersedes_miniapp_card(ctx) -> None:  # type: ignore[no-untyped-def]
    oid = (await _open(ctx))["opening_id"]
    service = await ctx.service()
    chat = await service.prepare(WIZARD, source=OpeningSource.WIZARD, chat_id=1, message_id=56)
    assert chat.opening is not None
    stale = await service.supersede_cards(chat.opening.id, 56)
    assert stale == []                                    # у карточки Mini App сообщения нет
    row = await _row(ctx, oid)
    assert row.status is OpeningStatus.EXPIRED_CARD and row.error_code == "SUPERSEDED"
    body = (await ctx.client.post(f"/api/open/{oid}/confirm", json={}, headers=ctx.h)).json()
    assert body["final"] is False and body["opening"]["status"] == "EXPIRED_CARD"
    assert "новая карточка" in body["opening"]["text"]
    assert ctx.exchange.posts() == [] and ctx.sent == []


async def test_new_miniapp_card_supersedes_previous(ctx) -> None:  # type: ignore[no-untyped-def]
    first = (await _open(ctx))["opening_id"]
    second = (await _open(ctx))["opening_id"]
    assert (await _row(ctx, first)).status is OpeningStatus.EXPIRED_CARD
    assert (await _row(ctx, second)).status is OpeningStatus.CARD
    assert ctx.bot.stripped == []
