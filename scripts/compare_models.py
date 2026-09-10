"""Сравнение моделей на одном и том же FactPack.

Запускается на сервере, где есть база и ключ. Строит факты один раз,
прогоняет через каждую модель и печатает отчёты рядом с расходом.

    docker compose exec bot python -m scripts.compare_models --user-id 1 --days 90

Кэш обходится намеренно: сравнение должно реально сходить в API.
Результаты в ai_reports не пишутся — это разовый эксперимент, а не отчёт.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.analysis.ai.pricing import currency, estimate_cost, pricing_for
from app.analysis.ai.prompts import SYSTEM_PROMPT, build_user_message
from app.analysis.ai.provider import AnthropicClient
from app.analysis.ai.schema import parse_report
from app.analysis.ai.service import estimate_tokens
from app.analysis.insights.facts import build_fact_pack
from app.analysis.insights.loader import load_facts
from app.analysis.insights.patterns import detect_all
from app.core.config import settings
from app.database.session import async_session_maker

SEPARATOR = "=" * 70


async def run_model(model: str, system: str, user: str, allowed: set[str]) -> None:
    """Один прогон. Печатает отчёт, расход и признаки проблем."""
    client = AnthropicClient(
        settings.anthropic_api_key.get_secret_value(),
        model=model,
        base_url=settings.ai_base_url,
        auth_scheme=settings.ai_auth_scheme,
    )
    try:
        response = await client.complete(
            system=system, user=user, max_tokens=settings.ai_max_output_tokens
        )
    except Exception as exc:
        print(f"\n{SEPARATOR}\n{model}: ОШИБКА {type(exc).__name__}: {exc}")
        return
    finally:
        await client.aclose()

    print(f"\n{SEPARATOR}\n{model}")
    print(SEPARATOR)

    try:
        report = parse_report(response.text)
    except ValueError as exc:
        print(f"схема НЕ прошла: {exc}")
        print("сырой ответ:")
        print(response.text[:2000])
        return

    filtered = report.known_codes_only(allowed)
    invented = len(report.insights) - len(filtered.insights)

    print(f"status: {report.status}")
    print(f"insights: {len(report.insights)}, выдуманных кодов: {invented}")
    print(f"токены: {response.usage.input_tokens} in / {response.usage.output_tokens} out")
    profile = settings.ai_pricing_profile
    cost = estimate_cost(
        model, response.usage.input_tokens, response.usage.output_tokens, profile=profile
    )
    print(f"стоимость: {cost:.4f} {currency(profile)}")
    print()
    print(report.summary)
    for item in filtered.insights:
        print(f"\n[{item.finding_code}] {item.title}")
        print(f"  {item.explanation}")
        print(f"  действие: {item.action}")
    if report.watch_next:
        print("\nпроверить дальше:")
        for line in report.watch_next:
            print(f"  - {line}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument(
        "--models",
        nargs="+",
        default=sorted(pricing_for(settings.ai_pricing_profile)),
        help="какие модели сравнивать",
    )
    parser.add_argument("--dump-facts", action="store_true", help="напечатать FactPack")
    args = parser.parse_args()

    period_end = datetime.now(tz=timezone.utc)
    period_start = period_end - timedelta(days=args.days)

    async with async_session_maker() as session:
        trades = await load_facts(
            session,
            user_id=args.user_id,
            period_start=period_start,
            period_end=period_end,
        )

    pack = build_fact_pack(trades, period_start, period_end)
    findings = detect_all(pack, trades)

    print(f"период: {period_start:%d.%m.%Y} — {period_end:%d.%m.%Y}")
    print(f"сделок: {pack.overall.trades}, findings: {len(findings)}")

    if not pack.is_sufficient or not findings:
        print("данных мало — сравнивать нечего, вызовов не будет")
        return

    user_message = build_user_message(pack, findings)
    prompt_tokens = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(user_message)
    print(f"промпт: ~{prompt_tokens} токенов")

    if args.dump_facts:
        print(json.dumps(pack.to_dict(), ensure_ascii=False, indent=2))

    allowed = {f.code for f in findings}
    for model in args.models:
        await run_model(model, SYSTEM_PROMPT, user_message, allowed)

    print(f"\n{SEPARATOR}")
    print("Один и тот же FactPack на всех моделях. Отличия — только в тексте.")


if __name__ == "__main__":
    asyncio.run(main())
