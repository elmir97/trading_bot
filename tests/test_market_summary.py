"""Пересказ «Анализа рынка» моделью (app/analysis/market_summary.py): модель
не добавляет чисел и советов, выключено по умолчанию, бюджет общий с
разбором журнала, пересказ не сдвигает паузу разбора журнала."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.analysis.ai.base import LLMResponse, LLMTimeoutError, LLMUsage
from app.analysis.market_summary import (
    FINGERPRINT_PREFIX,
    MarketSummaryService,
    fingerprint,
    plain,
    validate_summary,
)
from app.core.config import Settings

D = Decimal
SCREEN = (
    "🔎 <b>BTC-USDT</b> · цена 101.5\n\n"
    "<b>H1</b> ↑ выше EMA50/200 · HH/HL · RSI 61 · ATR 0.9% · объём ×1.4\n"
    "<b>D1</b> ↓ ниже EMA50/200 · LH/LL · RSI 38 · ATR 5.4% · объём ×1.1\n\n"
    "Сопротивление: 103.5 (H4)\nПоддержка: 98.25 (H4)\n"
    "Funding: +0.0100% · следующее через 3 ч 12 мин\nOpen interest: 908.4 млн USDT\n\n"
    "<i>Тренд — цена против EMA50/200; объём — последняя закрытая свеча к среднему за 20.</i>"
)
GOOD = (
    "На H1 цена выше EMA50/200 со структурой HH/HL и RSI 61, на D1 — ниже обеих EMA, "
    "RSI 38. Ближайшее сопротивление 103.5, поддержка 98.25; open interest 908.4 млн USDT."
)


class TestValidate:
    def test_good_summary_passes(self) -> None:
        assert validate_summary(GOOD, plain(SCREEN)) is None

    def test_invented_number_is_rejected(self) -> None:
        assert validate_summary("Цель — 120, RSI 61.", plain(SCREEN)) == "unknown_number"

    def test_number_with_comma_is_normalized(self) -> None:
        assert validate_summary("Поддержка 98,25.", plain(SCREEN)) is None

    @pytest.mark.parametrize(
        "text",
        ["Можно покупать от 98.25.", "Лонг выглядит лучше.", "Это сигнал на вход.",
         "Рекомендую подождать.", "SHORT по D1.", "Прогноз — рост."],
    )
    def test_advice_and_direction_rejected(self, text: str) -> None:
        assert validate_summary(text, plain(SCREEN)) == "forbidden_word"

    def test_too_long_and_empty(self) -> None:
        assert validate_summary("а" * 401, plain(SCREEN)) == "too_long"
        assert validate_summary("  ", plain(SCREEN)) == "empty"


class TestFingerprint:
    def test_prefix_and_length(self) -> None:
        key = fingerprint(plain(SCREEN))
        assert key.startswith(FINGERPRINT_PREFIX) and len(key) == 64

    def test_countdown_does_not_change_key(self) -> None:
        later = SCREEN.replace("3 ч 12 мин", "2 ч 59 мин")
        assert fingerprint(plain(SCREEN)) == fingerprint(plain(later))

    def test_numbers_change_key(self) -> None:
        assert fingerprint(plain(SCREEN)) != fingerprint(plain(SCREEN.replace("RSI 61", "RSI 62")))


class _Client:
    def __init__(self, text: str = GOOD, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._text = text
        self._error = error

    async def complete(self, *, system: str, user: str, max_tokens: int) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if self._error:
            raise self._error
        return LLMResponse(self._text, "claude-sonnet-5", LLMUsage(400, 80))

    async def aclose(self) -> None: ...


class _Repo:
    def __init__(self, spent: str = "0", cached: object = None) -> None:
        self.saved: list[dict] = []
        self._spent = D(spent)
        self._cached = cached

    async def get_by_fingerprint(self, user_id: int, fp: str):  # type: ignore[no-untyped-def]
        return self._cached

    async def spent_this_month(self, user_id: int) -> Decimal:
        return self._spent

    async def save(self, **kwargs):  # type: ignore[no-untyped-def]
        self.saved.append(kwargs)


def _settings(**overrides: object) -> Settings:
    fields: dict[str, object] = dict(
        database_url="postgresql+asyncpg://u:p@h/d", bot_token="1:x",
        encryption_key="7pGB0z4o2V5r8j9K1lq3m6N0aX2cY4eT6uW8yZ0bC1E=",
        ai_enabled=True, ai_market_summary_enabled=True,
    )
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


def _service(client=None, repo=None, **settings):  # type: ignore[no-untyped-def]
    return MarketSummaryService(
        client=client if client is not None else _Client(),
        reports_repo=repo if repo is not None else _Repo(),
        settings=_settings(**settings),
    )


class TestService:
    def test_flag_is_off_by_default(self) -> None:
        assert Settings.model_fields["ai_market_summary_enabled"].default is False

    @pytest.mark.parametrize(
        "flags", [{"ai_market_summary_enabled": False}, {"ai_enabled": False}]
    )
    async def test_disabled_means_no_call(self, flags: dict) -> None:
        client = _Client()
        assert await _service(client, **flags).summarize(1, SCREEN) is None
        assert client.calls == []

    async def test_no_client_means_none(self) -> None:
        service = MarketSummaryService(client=None, reports_repo=_Repo(), settings=_settings())
        assert await service.summarize(1, SCREEN) is None

    async def test_good_summary_returned_and_billed(self) -> None:
        client, repo = _Client(), _Repo()
        assert await _service(client, repo).summarize(7, SCREEN) == GOOD
        assert "<b>" not in client.calls[0]["user"]  # модель видит текст без HTML
        assert client.calls[0]["max_tokens"] == 300
        (row,) = repo.saved
        assert row["user_id"] == 7 and row["fingerprint"].startswith("market:")
        assert row["report_json"] == {"summary": GOOD, "rejected": None}
        assert row["cost_usd"] > 0 and row["input_tokens"] == 400

    async def test_rejected_summary_is_billed_but_not_shown(self) -> None:
        repo = _Repo()
        service = _service(_Client(text="Лонг от 98.25, цель 120."), repo)
        assert await service.summarize(1, SCREEN) is None
        assert repo.saved[0]["report_json"] == {"summary": None, "rejected": "forbidden_word"}

    async def test_cached_summary_skips_model(self) -> None:
        client = _Client()
        cached = SimpleNamespace(report_json={"summary": "старый пересказ", "rejected": None})
        result = await _service(client, _Repo(cached=cached)).summarize(1, SCREEN)
        assert result == "старый пересказ"
        assert client.calls == []

    async def test_budget_exhausted(self) -> None:
        client = _Client()
        service = _service(client, _Repo(spent="5"), ai_monthly_budget_usd=D("5"))
        assert await service.summarize(1, SCREEN) is None
        assert client.calls == []

    async def test_llm_error_falls_back_to_none(self) -> None:
        repo = _Repo()
        service = _service(_Client(error=LLMTimeoutError("slow")), repo)
        assert await service.summarize(1, SCREEN) is None
        assert repo.saved == []


# --- БД: пересказ не сдвигает паузу разбора журнала ---------------------------


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")
async def test_market_rows_do_not_affect_journal_cooldown(unique_telegram_id) -> None:  # type: ignore[no-untyped-def]
    from app.database.models.ai_report import AIReport
    from app.database.repositories.ai_report import AIReportRepository
    from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
    from app.database.repositories.user import UserRepository
    from app.database.session import Database
    from app.services.user_service import UserService
    from tests.conftest import cleanup_user

    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        try:
            repo = AIReportRepository(session)
            now = datetime.now(UTC)
            journal_at = now - timedelta(days=2)

            def row(fp: str, at: datetime, cost: str) -> AIReport:
                return AIReport(
                    user_id=user.id, fingerprint=fp, period_start=at, period_end=at,
                    facts_json={}, report_json={}, model="claude-sonnet-5",
                    input_tokens=1, output_tokens=1, cost_usd=D(cost), created_at=at,
                )

            session.add(row("journal-fp", journal_at, "0.10"))
            session.add(row(FINGERPRINT_PREFIX + "abc", now, "0.02"))
            await session.flush()

            last = await repo.last_created_at(user.id)
            assert last is not None and abs(last - journal_at) < timedelta(seconds=1)
            # Бюджет — общий: обе строки в расходах месяца (если обе в этом месяце).
            spent = await repo.spent_this_month(user.id)
            assert spent >= D("0.02")
        finally:
            await cleanup_user(session, user)
    await db.dispose()


async def test_middleware_puts_market_summary_next_to_ai_service() -> None:
    from app.bot.middlewares.ai_service import AIServiceMiddleware

    middleware = AIServiceMiddleware(_Client(), _settings())  # type: ignore[arg-type]
    seen: dict = {}

    async def handler(event, data):  # type: ignore[no-untyped-def]
        seen.update(data)

    await middleware(handler, object(), {"session": object()})  # type: ignore[arg-type]
    assert isinstance(seen["market_summary"], MarketSummaryService)
    assert seen["market_summary"].enabled
    assert "ai_service" in seen

    seen.clear()
    await middleware(handler, object(), {})  # type: ignore[arg-type]
    assert "market_summary" not in seen
