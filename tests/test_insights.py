"""Тесты этапа 11: агрегаты, детекторы, кэш, лимиты, фолбэк."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.analysis.ai.base import LLMResponse, LLMTimeoutError, LLMUsage
from app.analysis.ai.schema import parse_report
from app.analysis.ai.pricing import UnknownModel, UnknownProfile, estimate_cost, is_known
from app.analysis.ai.service import AIAnalysisService
from app.analysis.insights import rules
from app.analysis.insights.facts import (
    TradeFact,
    build_fact_pack,
    build_group,
    longest_streak,
    max_drawdown,
)
from app.analysis.insights.patterns import detect_all, detect_overtrading

START = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def make_trade(
    idx: int,
    pnl: str,
    *,
    symbol: str = "BTC-USDT",
    strategy: str = "breakout_retest",
    hours: int = 0,
    duration: int = 2,
    realized_r: str | None = None,
    planned_rr: str | None = None,
    mistakes: tuple[str, ...] = (),
) -> TradeFact:
    opened = START + timedelta(hours=hours)
    return TradeFact(
        id=idx,
        symbol=symbol,
        side="LONG",
        strategy=strategy,
        timeframe="H1",
        opened_at=opened,
        closed_at=opened + timedelta(hours=duration),
        net_pnl=Decimal(pnl),
        risk_amount=Decimal("100"),
        planned_rr=Decimal(planned_rr) if planned_rr else Decimal("3"),
        realized_r=Decimal(realized_r) if realized_r else Decimal(pnl) / Decimal("100"),
        mistakes=mistakes,
    )


class TestAggregates:
    def test_group_metrics(self):
        trades = [make_trade(1, "100"), make_trade(2, "-50"), make_trade(3, "200")]
        stat = build_group("x", trades)
        assert stat.trades == 3
        assert stat.wins == 2
        assert stat.net_pnl == Decimal("250")
        assert stat.profit_factor == Decimal("300") / Decimal("50")

    def test_profit_factor_none_without_losses(self):
        stat = build_group("x", [make_trade(1, "10"), make_trade(2, "20")])
        assert stat.profit_factor is None

    def test_empty_group_is_safe(self):
        stat = build_group("x", [])
        assert stat.trades == 0 and stat.net_pnl == Decimal("0")

    def test_max_drawdown(self):
        trades = [
            make_trade(1, "100", hours=1),
            make_trade(2, "-40", hours=2),
            make_trade(3, "-30", hours=3),
            make_trade(4, "50", hours=4),
        ]
        assert max_drawdown(trades) == Decimal("-70")

    def test_max_drawdown_never_positive(self):
        assert max_drawdown([make_trade(1, "100")]) == Decimal("0")

    def test_loss_streak(self):
        trades = [
            make_trade(1, "-10", hours=1),
            make_trade(2, "-10", hours=2),
            make_trade(3, "50", hours=3),
            make_trade(4, "-10", hours=4),
        ]
        assert longest_streak(trades, wins=False) == 2
        assert longest_streak(trades, wins=True) == 1


class TestGrouping:
    def test_small_groups_filtered_out(self):
        trades = [make_trade(i, "10", hours=i) for i in range(20)]
        trades.append(make_trade(99, "-500", symbol="DOGE-USDT", hours=99))
        pack = build_fact_pack(trades, START, START + timedelta(days=5))
        symbols = {g.key for g in pack.by_symbol}
        assert "DOGE-USDT" not in symbols

    def test_fingerprint_is_stable_and_content_sensitive(self):
        trades = [make_trade(i, "10", hours=i) for i in range(20)]
        end = START + timedelta(days=5)
        a = build_fact_pack(trades, START, end).fingerprint()
        b = build_fact_pack(list(trades), START, end).fingerprint()
        c = build_fact_pack(trades + [make_trade(50, "-999", hours=50)], START, end).fingerprint()
        assert a == b
        assert a != c

    def test_fact_pack_is_json_serialisable(self):
        trades = [make_trade(i, "10", hours=i) for i in range(20)]
        pack = build_fact_pack(trades, START, START + timedelta(days=5))
        json.dumps(pack.to_dict(), ensure_ascii=False)


class TestDetectors:
    def test_overtrading_needs_enough_sample(self):
        trades = [make_trade(i, "-10", hours=i) for i in range(3)]
        assert detect_overtrading(trades) == []

    def test_overtrading_detected(self):
        heavy = [make_trade(i, "-20", hours=i) for i in range(10)]
        light = [
            make_trade(100 + i, "50", hours=24 * (i + 2) + 1) for i in range(10)
        ]
        findings = detect_overtrading(heavy + light)
        assert findings and findings[0].code == "OVERTRADING"
        assert findings[0].sample_size == 10

    def test_mistake_cost_only_for_losses(self):
        trades = [
            make_trade(i, "-30", hours=i, mistakes=("FOMO",))
            for i in range(rules.MIN_GROUP_SAMPLE)
        ]
        pack = build_fact_pack(trades, START, START + timedelta(days=2))
        codes = {f.code for f in detect_all(pack, trades)}
        assert "MISTAKE_COST" in codes

    def test_findings_sorted_by_absolute_effect(self):
        trades = [make_trade(i, "-30", hours=i, mistakes=("FOMO",)) for i in range(12)]
        pack = build_fact_pack(trades, START, START + timedelta(days=2))
        findings = detect_all(pack, trades)
        effects = [abs(f.effect_pnl) for f in findings]
        assert effects == sorted(effects, reverse=True)


class TestSchema:
    def test_parses_markdown_wrapped_json(self):
        raw = '```json\n{"status": "OK", "summary": "ок", "insights": [], "watch_next": []}\n```'
        assert parse_report(raw).status == "OK"

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_report("не json")

    def test_unknown_finding_codes_dropped(self):
        raw = json.dumps(
            {
                "status": "OK",
                "summary": "s",
                "insights": [
                    {"finding_code": "REAL", "title": "t", "explanation": "e", "action": "a"},
                    {"finding_code": "ПРИДУМАНО", "title": "t", "explanation": "e", "action": "a"},
                ],
                "watch_next": [],
            }
        )
        report = parse_report(raw).known_codes_only({"REAL"})
        assert [i.finding_code for i in report.insights] == ["REAL"]


class FakeRepo:
    def __init__(self, spent=Decimal("0"), cached=None):
        self.spent = spent
        self.cached = cached
        self.saved = []

    async def get_by_fingerprint(self, user_id, fingerprint):
        return self.cached

    async def spent_this_month(self, user_id):
        return self.spent

    async def save(self, **kwargs):
        self.saved.append(kwargs)


class FakeClient:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = 0

    async def complete(self, *, system, user, max_tokens):
        self.calls += 1
        if self.error:
            raise self.error
        return LLMResponse(text=self.text, model="claude-sonnet-5", usage=LLMUsage(5000, 900))

    async def aclose(self):
        pass


def build_service(client, repo, **overrides):
    kwargs = dict(
        client=client,
        reports_repo=repo,
        model="claude-sonnet-5",
        max_output_tokens=1500,
        max_input_tokens=20000,
        monthly_budget_usd=Decimal("5"),
        enabled=True,
        pricing_profile="anthropic",
    )
    kwargs.update(overrides)
    return AIAnalysisService(**kwargs)


def losing_trades(n=25):
    return [make_trade(i, "-30", hours=i, mistakes=("FOMO",)) for i in range(n)]


GOOD_JSON = json.dumps(
    {"status": "OK", "summary": "разбор", "insights": [], "watch_next": []}
)


class TestService:
    @pytest.mark.asyncio
    async def test_insufficient_data_never_calls_api(self):
        client = FakeClient(text=GOOD_JSON)
        service = build_service(client, FakeRepo())
        result = await service.analyze(
            user_id=1,
            trades=[make_trade(1, "10")],
            period_start=START,
            period_end=START + timedelta(days=1),
        )
        assert client.calls == 0
        assert result.ai is None
        assert result.ai_error == "INSUFFICIENT_DATA"

    @pytest.mark.asyncio
    async def test_budget_guard_blocks_call(self):
        client = FakeClient(text=GOOD_JSON)
        service = build_service(client, FakeRepo(spent=Decimal("5")))
        result = await service.analyze(
            user_id=1,
            trades=losing_trades(),
            period_start=START,
            period_end=START + timedelta(days=3),
        )
        assert client.calls == 0
        assert result.ai_error == "LLMBudgetExceeded"
        assert result.pack.overall.trades == 25

    @pytest.mark.asyncio
    async def test_oversized_prompt_blocked(self):
        client = FakeClient(text=GOOD_JSON)
        service = build_service(client, FakeRepo(), max_input_tokens=10)
        result = await service.analyze(
            user_id=1,
            trades=losing_trades(),
            period_start=START,
            period_end=START + timedelta(days=3),
        )
        assert client.calls == 0
        assert result.ai_error == "LLMBudgetExceeded"

    @pytest.mark.asyncio
    async def test_provider_failure_falls_back_to_numbers(self):
        client = FakeClient(error=LLMTimeoutError("timeout"))
        service = build_service(client, FakeRepo())
        result = await service.analyze(
            user_id=1,
            trades=losing_trades(),
            period_start=START,
            period_end=START + timedelta(days=3),
        )
        assert result.ai is None
        assert result.ai_error == "LLMTimeoutError"
        assert result.findings

    @pytest.mark.asyncio
    async def test_successful_call_is_saved_with_cost(self):
        repo = FakeRepo()
        client = FakeClient(text=GOOD_JSON)
        service = build_service(client, repo)
        result = await service.analyze(
            user_id=7,
            trades=losing_trades(),
            period_start=START,
            period_end=START + timedelta(days=3),
        )
        assert result.ai is not None and result.ai.status == "OK"
        assert len(repo.saved) == 1
        assert repo.saved[0]["user_id"] == 7
        assert repo.saved[0]["cost_usd"] > Decimal("0")

    @pytest.mark.asyncio
    async def test_cache_hit_skips_api(self):
        class Cached:
            report_json = {"status": "OK", "summary": "из кэша", "insights": [], "watch_next": []}

        client = FakeClient(text=GOOD_JSON)
        service = build_service(client, FakeRepo(cached=Cached()))
        result = await service.analyze(
            user_id=1,
            trades=losing_trades(),
            period_start=START,
            period_end=START + timedelta(days=3),
        )
        assert client.calls == 0
        assert result.cached is True
        assert result.ai.summary == "из кэша"


class TestPricing:
    def test_sonnet_cost(self):
        assert estimate_cost("claude-sonnet-5", 1_000_000, 0) == Decimal("3")
        assert estimate_cost("claude-sonnet-5", 0, 1_000_000) == Decimal("15")

    def test_haiku_cost(self):
        assert estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0) == Decimal("1")
        assert estimate_cost("claude-haiku-4-5-20251001", 0, 1_000_000) == Decimal("5")

    def test_proxyapi_profile_uses_rub_prices(self):
        assert estimate_cost(
            "claude-sonnet-5", 1_000_000, 0, profile="proxyapi"
        ) == Decimal("600")
        assert estimate_cost(
            "claude-sonnet-5", 0, 1_000_000, profile="proxyapi"
        ) == Decimal("3030")

    def test_profiles_are_independent(self):
        assert estimate_cost("claude-sonnet-5", 1_000_000, 0) == Decimal("3")

    def test_unknown_profile_raises(self):
        with pytest.raises(UnknownProfile):
            estimate_cost("claude-sonnet-5", 1, 1, profile="что-то")

    def test_model_known_in_one_profile_only(self):
        assert is_known("claude-haiku-4-5-20251001", "anthropic")
        assert not is_known("claude-haiku-4-5-20251001", "proxyapi")

    def test_unknown_model_raises(self):
        with pytest.raises(UnknownModel):
            estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0, profile="proxyapi")


class TestRender:
    def test_render_without_ai_still_shows_numbers(self):
        from app.analysis import reports
        from app.analysis.ai.service import JournalInsights

        trades = losing_trades()
        pack = build_fact_pack(trades, START, START + timedelta(days=3))
        text = reports.render(
            JournalInsights(pack=pack, findings=detect_all(pack, trades), ai=None,
                            ai_error="LLMTimeoutError")
        )
        assert "Разбор журнала" in text
        assert "Сделок: 25" in text
        assert "не прогноз" in text
