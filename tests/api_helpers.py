"""Общее для тестов API Mini App: подписанный initData, клиент ASGI, фейки."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest_asyncio

from app.api.auth import sign
from app.api.deps import ApiState
from app.api.main import create_app
from app.core.config import Settings
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    Balance,
    Kline,
    LeverageInfo,
    OpenInterest,
    PremiumIndex,
    SymbolInfo,
    Ticker,
)
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode
from tests.conftest import cleanup_user

TOKEN = "0:apitest"


def init_data(
    telegram_id: int, *, token: str = TOKEN, auth_date: int | None = None,
    extra: dict[str, str] | None = None, user: Any = None,
) -> str:
    """initData, подписанный как Telegram: поля → hash по data_check_string."""
    fields = {
        "query_id": "AAH-test",
        "user": json.dumps(user if user is not None else {"id": telegram_id, "first_name": "T"},
                           separators=(",", ":")),
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        **(extra or {}),
    }
    fields["hash"] = sign(list(fields.items()), token)
    return urlencode(fields)


def auth(telegram_id: int, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"tma {init_data(telegram_id, **kwargs)}"}


class FakeClient:
    """Клиент на ключе пользователя: баланс и плечо."""

    def __init__(self, equity: Decimal = Decimal("1500.4567"), fail: Exception | None = None):
        self.equity = equity
        self.fail = fail
        self.closed = False
        self.leverage_calls = 0

    async def get_leverage(self, symbol: str) -> LeverageInfo:
        self.leverage_calls += 1
        if self.fail is not None:
            raise self.fail
        return LeverageInfo(symbol, 10, 5, 75, 50)

    async def get_balance(self) -> Balance:
        if self.fail is not None:
            raise self.fail
        return Balance(asset="VST", available=self.equity - 25, used_margin=Decimal(25),
                       unrealized_pnl=Decimal(0), equity=self.equity)

    async def close(self) -> None:
        self.closed = True


XRP = SymbolInfo("XRP-USDT", 4, 0, Decimal(2), Decimal(2))
SYMBOLS = [
    XRP,
    SymbolInfo("XRPUP-USDT", 5, 1, Decimal(1), Decimal(2)),
    SymbolInfo("BTC-USDT", 1, 4, Decimal("0.0001"), Decimal(2)),
    SymbolInfo("BTC-USDC", 1, 4, Decimal("0.0001"), Decimal(2)),
]
T0 = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)


class FakePublic:
    """Публичный клиент: контракты, тикер, свечи, фандинг, ОИ. calls — по методам."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.fail: dict[str, Exception] = {}
        self.closed = 0

    def _hit(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.fail:
            raise self.fail[name]

    async def get_symbols(self) -> list[SymbolInfo]:
        self._hit("symbols")
        return SYMBOLS

    async def get_ticker(self, symbol: str) -> Ticker:
        self._hit("ticker")
        return Ticker(symbol, Decimal("1.4952"), T0, open_price=Decimal("1.4829"),
                      price_change=Decimal("0.01234"), price_change_percent=Decimal("0.8321"),
                      quote_volume=Decimal("31820000.456"))

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[Kline]:
        self._hit(f"klines:{interval}")
        step = timedelta(minutes=5)
        return [
            Kline(T0 - step, Decimal("1.49"), Decimal("1.5"), Decimal("1.48"),
                  Decimal("1.495"), Decimal(10), T0),
            Kline(T0, Decimal("1.4960"), Decimal("1.49711"), Decimal("1.4949"),
                  Decimal("1.4952"), Decimal(3), T0 + step),
        ]

    async def get_premium_index(self, symbol: str) -> PremiumIndex:
        self._hit("premium")
        return PremiumIndex(symbol, Decimal("1.4951"), Decimal("1.4950"), Decimal("0.0001"),
                            T0 + timedelta(hours=2))

    async def get_open_interest(self, symbol: str) -> OpenInterest:
        self._hit("oi")
        return OpenInterest(symbol, Decimal("21940000.1"), T0)

    async def close(self) -> None:
        self.closed += 1


class FakeFactory:
    def __init__(self, client: Any = None, *, has_key: bool = True,
                 public: FakePublic | None = None) -> None:
        self.client = client or FakeClient()
        self.has_key = has_key
        self.public = public or FakePublic()

    def public_client(self) -> FakePublic:
        return self.public

    async def get_credentials(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
        return SimpleNamespace(is_read_only=False, user_id=user_id) if self.has_key else None

    async def for_user(self, session, user_id, exchange_name="bingx", mode=None):  # type: ignore[no-untyped-def]
        return self.client


@pytest_asyncio.fixture(name="api")
async def api_fixture(unique_telegram_id):  # type: ignore[no-untyped-def]
    tid = unique_telegram_id()
    settings = Settings(  # type: ignore[call-arg]
        bot_token=TOKEN, trading_execution_enabled=True, bingx_trading_mode="demo",
        miniapp_allowed_telegram_ids=str(tid),
    )
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=tid)
        user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
        await session.commit()
        uid = user.id

    def state(**kwargs):  # type: ignore[no-untyped-def]
        s = settings.model_copy(update=kwargs.pop("settings", {}))
        return ApiState(settings=s, db=db, redis=None, factory=kwargs.pop("factory", FakeFactory()))

    yield SimpleNamespace(tid=tid, uid=uid, state=state, settings=settings)
    async with db.session() as session:
        fresh = await UserRepository(session).get_by_id(uid)
        if fresh is not None:
            await cleanup_user(session, fresh)
        await session.commit()
    await db.dispose()


def client_for(state: ApiState) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(state))
    return httpx.AsyncClient(transport=transport, base_url="http://test")
