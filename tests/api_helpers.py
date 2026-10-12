"""Общее для тестов API Mini App: подписанный initData, клиент ASGI, фейки."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx

from app.api.auth import sign
from app.api.deps import ApiState
from app.api.main import create_app
from app.exchanges.base import Balance

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
    def __init__(self, equity: Decimal = Decimal("1500.4567"), fail: Exception | None = None):
        self.equity = equity
        self.fail = fail
        self.closed = False

    async def get_balance(self) -> Balance:
        if self.fail is not None:
            raise self.fail
        return Balance(asset="VST", available=self.equity - 25, used_margin=Decimal(25),
                       unrealized_pnl=Decimal(0), equity=self.equity)

    async def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, client: Any = None, *, has_key: bool = True) -> None:
        self.client = client or FakeClient()
        self.has_key = has_key

    async def get_credentials(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
        return SimpleNamespace(is_read_only=False, user_id=user_id) if self.has_key else None

    async def for_user(self, session, user_id, exchange_name="bingx", mode=None):  # type: ignore[no-untyped-def]
        return self.client


def client_for(state: ApiState) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(state))
    return httpx.AsyncClient(transport=transport, base_url="http://test")
