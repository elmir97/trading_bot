"""Локальный стенд фронта Mini App (docs/miniapp-plan.md §9а).

API (`app.api.main:create_app`) с подменённой авторизацией и фейковой биржей
плюс статика `miniapp/` с теми же заголовками безопасности, что выставит Caddy
(§5, §6) — проверка в Chrome: ноль нарушений CSP, экраны.

    python -m scripts.miniapp_dev [--port 8765]

Страховки (отказ — exit 2 с причиной):
- слушает только 127.0.0.1, ключа --host нет;
- BOT_TOKEN не тестовый (id бота до «:» — не 0);
- скрипт или текущий каталог под /opt/trading_bot;
- BINGX_TRADING_MODE не demo;
- база не trading_bot_test (пишет пользователя стенда и открытия).

Обход авторизации — только здесь (`dependency_overrides`), в `app/` его нет:
любой запрос идёт от пользователя telegram_id=434343 (зарезервирован под стенд,
при выходе по Ctrl+C убирается; после жёсткого kill остаётся — следующий запуск
его переиспользует и уберёт). Биржа — `tests.opening_fakes.OpeningExchange` + свечи,
фандинг и ОИ; бота нет (bot=None) — в Telegram ничего не уходит. В образ скрипт
не попадает (`.dockerignore`).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePath
from types import SimpleNamespace

import uvicorn
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import make_url

from app.api.deps import ApiState, Session, current_user
from app.api.main import create_app
from app.core.config import Settings, get_settings
from app.database.models.user import User
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import Kline, OpenInterest, PremiumIndex, Ticker
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode
from tests.conftest import cleanup_user
from tests.opening_fakes import OpeningExchange

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEV_TELEGRAM_ID = 434343
REQUIRED_DB_NAME = "trading_bot_test"
PROD_ROOT = "/opt/trading_bot"
MINIAPP_DIR = Path(__file__).resolve().parents[1] / "miniapp"

# Как в Caddyfile (§5, §6); HSTS — только на HTTPS, у стенда его нет.
CSP = (
    "default-src 'self'; script-src 'self' https://telegram.org; connect-src 'self'; "
    "img-src 'self' data:; style-src 'self'; "
    "frame-ancestors https://web.telegram.org https://*.telegram.org"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_STEP = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


class StandRefusedError(Exception):
    """Признак прода или не той среды — стенд не запускается."""


def _under_prod(path: PurePath) -> bool:
    text = path.as_posix()
    return text == PROD_ROOT or text.startswith(PROD_ROOT + "/")


def check_environment(settings: Settings, *, script: PurePath, cwd: PurePath) -> None:
    token_id = settings.bot_token.get_secret_value().split(":", 1)[0]
    if token_id != "0":
        raise StandRefusedError("BOT_TOKEN не тестовый: id бота не 0")
    if _under_prod(script) or _under_prod(cwd):
        raise StandRefusedError(f"запуск под {PROD_ROOT} — это прод")
    if settings.bingx_trading_mode != "demo":
        raise StandRefusedError("BINGX_TRADING_MODE не demo")
    name = make_url(settings.database_url.get_secret_value()).database
    if name != REQUIRED_DB_NAME:
        raise StandRefusedError(f"база {name!r}, нужна {REQUIRED_DB_NAME}")


class DevExchange(OpeningExchange):
    """Фейк ядра + то, что нужно экрану рынка: тикер с изменениями, свечи,
    фандинг, открытый интерес. Свечи — случайное блуждание, которое кончается
    на last (иначе график прыгает к цене), одно и то же для таймфрейма при
    каждой перезагрузке; сетка свечей — по границам интервала, как у биржи."""

    async def get_ticker(self, symbol: str, *, max_retries: int | None = None) -> Ticker:
        self._read("ticker", symbol)
        open_price = self.last - Decimal("0.0123")
        change = self.last - open_price
        return Ticker(symbol, self.last, datetime.now(UTC), open_price=open_price,
                      price_change=change,
                      price_change_percent=(change / open_price * 100).quantize(Decimal("0.0001")),
                      quote_volume=Decimal("31820000.45"))

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[Kline]:
        self._read("klines", interval)
        step = timedelta(minutes=_STEP.get(interval, 5))
        epoch = datetime(2000, 1, 1, tzinfo=UTC)
        now = epoch + (datetime.now(UTC) - epoch) // step * step   # открытие текущей свечи
        rng = random.Random(f"{DEV_TELEGRAM_ID}:{interval}")
        close = self.last
        out: list[Kline] = []
        for i in range(limit):                                  # от текущей свечи назад
            open_ = (close * Decimal(str(1 + rng.uniform(-0.004, 0.004)))).quantize(
                Decimal("0.0001"))
            high = max(open_, close) + Decimal("0.0010")
            low = min(open_, close) - Decimal("0.0010")
            t = now - step * i
            out.append(Kline(t, open_, high, low, close, Decimal(rng.randint(1, 900)), t + step))
            close = open_
        return out[::-1]

    async def get_premium_index(self, symbol: str) -> PremiumIndex:
        self._read("premium", symbol)
        return PremiumIndex(symbol, self.mark, self.last, Decimal("0.0001"),
                            datetime.now(UTC) + timedelta(hours=2, minutes=15))

    async def get_open_interest(self, symbol: str) -> OpenInterest:
        self._read("oi", symbol)
        return OpenInterest(symbol, Decimal("21940000.1"), datetime.now(UTC))


class DevFactory:
    """ExchangeFactory стенда: и ключевой, и публичный клиент — одна DevExchange."""

    def __init__(self, exchange: DevExchange) -> None:
        self.exchange = exchange

    async def get_credentials(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
        return SimpleNamespace(is_read_only=False, permissions_checked_at=datetime.now(UTC),
                               user_id=user_id, mode=mode)

    async def for_user(self, session, user_id, exchange_name="bingx", mode=None):  # type: ignore[no-untyped-def]
        return self.exchange

    def public_client(self) -> DevExchange:
        return self.exchange


# telegram_id у функций пользователя — для тестов: они работают со своим id и не трогают
# пользователя запущенного стенда (общая база trading_bot_test).


async def ensure_dev_user(
    db: Database, settings: Settings, telegram_id: int = DEV_TELEGRAM_ID,
) -> int:
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=telegram_id)
        user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
        await session.commit()
        return user.id


async def remove_dev_user(db: Database, telegram_id: int = DEV_TELEGRAM_ID) -> None:
    async with db.session() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if user is not None:
            await cleanup_user(session, user)
        await session.commit()


def build_app(state: ApiState, telegram_id: int = DEV_TELEGRAM_ID) -> FastAPI:
    async def dev_user(session: Session) -> User:
        """Подмена current_user: initData не проверяется — всегда пользователь стенда."""
        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if user is None:
            raise RuntimeError("пользователя стенда нет — ensure_dev_user не вызван")
        return user

    app = create_app(state)
    app.dependency_overrides[current_user] = dev_user

    @app.post("/dev/reset")
    async def dev_reset() -> dict[str, bool]:
        """Чистый лист для сценариев браузера (--scenario): пользователь стенда заново
        (без сделок, открытий и лимита сделок за день), новая фейковая биржа (без
        позиций и ордеров), пустой кэш процесса. Только стенд — в app/ этого нет."""
        await remove_dev_user(state.db, telegram_id)
        await ensure_dev_user(state.db, state.settings, telegram_id)
        if isinstance(state.factory, DevFactory):
            state.factory.exchange = DevExchange()
        state.market_cache.invalidate()
        return {"ok": True}

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    # После роутера: /api/* — API, остальное — статика (как handle /api/* у Caddy).
    app.mount("/", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
    return app


def server_config(app: FastAPI, port: int) -> uvicorn.Config:
    return uvicorn.Config(app, host=HOST, port=port, log_level="info")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args(argv)


async def serve(settings: Settings, port: int) -> None:
    db = Database(settings)
    try:
        await ensure_dev_user(db, settings)
        state = ApiState(settings=settings, db=db, redis=FakeRedis(decode_responses=True),
                         factory=DevFactory(DevExchange()))
        print(f"Стенд Mini App: http://{HOST}:{port}/  (Ctrl+C — стоп)")
        await uvicorn.Server(server_config(build_app(state), port)).serve()
    finally:
        await remove_dev_user(db)
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = get_settings()
    try:
        check_environment(settings, script=Path(__file__).resolve(), cwd=Path(os.getcwd()))
    except StandRefusedError as exc:
        print(f"✗ Стенд не запущен: {exc}", file=sys.stderr)
        return 2
    asyncio.run(serve(settings, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

