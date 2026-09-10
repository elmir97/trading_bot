"""Рендер разбора журнала в сообщение Telegram.

Детерминированная часть выводится всегда. Блок модели добавляется, если
он есть, и явно помечается — пользователь должен понимать, где цифры,
а где интерпретация.
"""

from __future__ import annotations

from decimal import Decimal

from app.analysis.ai.service import JournalInsights
from app.analysis.insights.patterns import Finding

CONFIDENCE_LABEL = {"high": "высокая", "medium": "средняя", "low": "низкая"}

AI_ERROR_TEXT = {
    "INSUFFICIENT_DATA": "Мало данных для разбора — нужно минимум 20 закрытых сделок за период.",
    "LLMDisabled": "AI-разбор выключен в настройках.",
    "LLMBudgetExceeded": "Достигнут месячный лимит расходов на AI. Цифры ниже посчитаны без модели.",
    "LLMAuthError": "AI-провайдер отклонил ключ. Цифры ниже посчитаны без модели.",
    "LLMTimeoutError": "AI-провайдер не ответил. Цифры ниже посчитаны без модели.",
    "LLMRateLimitError": "AI-провайдер ограничил запросы. Попробуй позже.",
}
AI_ERROR_DEFAULT = "AI-разбор недоступен. Цифры ниже посчитаны без модели."


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))} USDT"


def _finding_line(finding: Finding) -> str:
    parts = [f"• <b>{finding.title}</b>"]
    parts.append(f"  {_money(finding.effect_pnl)}, сделок: {finding.sample_size}")
    if finding.effect_r is not None:
        parts.append(f"  отклонение: {finding.effect_r.quantize(Decimal('0.01'))} R")
    parts.append(f"  достоверность: {CONFIDENCE_LABEL.get(finding.confidence, finding.confidence)}")
    return "\n".join(parts)


def render(insights: JournalInsights, *, max_findings: int = 6) -> str:
    """Собирает готовый HTML-текст для отправки."""
    pack = insights.pack
    overall = pack.overall

    lines = [
        f"🧠 <b>Разбор журнала</b>",
        f"{pack.period_start:%d.%m.%Y} — {pack.period_end:%d.%m.%Y}",
        "",
        f"Сделок: {overall.trades} · Win rate: {overall.win_rate.quantize(Decimal('0.01'))}%",
        f"Итог: {_money(overall.net_pnl)} · Просадка: {_money(pack.max_drawdown)}",
    ]
    if overall.profit_factor is not None:
        lines.append(f"Profit factor: {overall.profit_factor.quantize(Decimal('0.01'))}")
    if overall.expectancy_r is not None:
        lines.append(f"Expectancy: {overall.expectancy_r.quantize(Decimal('0.01'))} R на сделку")

    if insights.findings:
        lines += ["", "<b>Найденные закономерности</b>"]
        for finding in insights.findings[:max_findings]:
            lines.append(_finding_line(finding))

    if insights.ai is not None and insights.ai.status == "OK":
        lines += ["", "<b>Интерпретация</b>"]
        if insights.ai.summary:
            lines.append(insights.ai.summary)
        for item in insights.ai.insights:
            lines += ["", f"<b>{item.title}</b>", item.explanation, f"→ {item.action}"]
        if insights.ai.watch_next:
            lines += ["", "<b>Проверить на следующем периоде</b>"]
            lines += [f"• {item}" for item in insights.ai.watch_next]
        if insights.cached:
            lines += ["", "<i>Отчёт из кэша: факты с прошлого разбора не изменились.</i>"]
    elif insights.ai_error:
        lines += ["", f"<i>{AI_ERROR_TEXT.get(insights.ai_error, AI_ERROR_DEFAULT)}</i>"]

    lines += [
        "",
        "<i>Это разбор прошлых сделок, а не прогноз. Закономерность на выборке "
        "в несколько десятков сделок может оказаться случайностью.</i>",
    ]
    return "\n".join(lines)
