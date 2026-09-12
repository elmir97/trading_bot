"""Статистика, отчёты, просадка и анализ ошибок."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.formatting import fmt_money, fmt_num, fmt_ratio
from app.bot.keyboards.main import MenuCallback, back_to_main
from app.bot.keyboards.trade import plural_trades
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.services.statistics_service import StatisticsService, period_bounds
from app.trading.risk import tz_offset_for
from app.trading.statistics import (
    Statistics,
    calculate_statistics,
    group_by,
    group_by_hour,
    group_by_weekday,
)

router = Router(name="statistics")

PERIOD_LABELS = {
    "today": "Сегодня",
    "week": "Неделя",
    "month": "Месяц",
    "all": "Всё время",
}


class StatsCB:
    PERIOD = "stats:period:"
    BREAKDOWN = "stats:by:"


def period_keyboard(active: str = "all") -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for key, label in PERIOD_LABELS.items():
        mark = "• " if key == active else ""
        builder.button(text=f"{mark}{label}", callback_data=f"{StatsCB.PERIOD}{key}")
    builder.adjust(4)
    builder.row(
        InlineKeyboardButton(
            text="📊 По инструментам", callback_data=f"{StatsCB.BREAKDOWN}symbol"
        ),
        InlineKeyboardButton(
            text="🎯 По стратегиям", callback_data=f"{StatsCB.BREAKDOWN}strategy"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="↕️ LONG / SHORT", callback_data=f"{StatsCB.BREAKDOWN}side"
        ),
        InlineKeyboardButton(
            text="🕐 По времени", callback_data=f"{StatsCB.BREAKDOWN}time"
        ),
    )
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN))
    return builder


async def _tz_offset(session: AsyncSession, user_id: int) -> int:
    """Сдвиг таймзоны пользователя в часах. Таблица смещений — в app.trading.risk."""
    settings = await UserRepository(session).get_settings(user_id)
    return tz_offset_for(settings.timezone if settings else None)


def render_statistics(stats: Statistics, title: str) -> str:
    if stats.total_trades == 0:
        return f"<b>{title}</b>\n\nЗакрытых сделок за период нет."

    lines = [
        f"<b>{title}</b>",
        "",
        f"Сделок: {stats.total_trades}",
        f"Прибыльных: {stats.wins} · убыточных: {stats.losses}",
        f"Win Rate: {fmt_ratio(stats.win_rate)}%",
        "",
        f"<b>PnL: {fmt_money(stats.total_pnl)} USDT</b>",
        f"Средняя сделка: {fmt_money(stats.average_pnl)}",
        f"Средняя прибыль: {fmt_money(stats.average_win)}",
        f"Средний убыток: −{fmt_num(stats.average_loss)}",
        "",
    ]

    if stats.profit_factor is not None:
        lines.append(f"Profit Factor: {fmt_ratio(stats.profit_factor)}")
    else:
        lines.append("Profit Factor: — (убыточных сделок нет)")

    lines.append(f"Expectancy: {fmt_money(stats.expectancy)} на сделку")
    if stats.average_rr is not None:
        lines.append(f"Средний R: {fmt_ratio(stats.average_rr)}")

    lines += [
        "",
        f"Лучшая: {fmt_money(stats.largest_win)}",
        f"Худшая: {fmt_money(stats.largest_loss)}",
        f"Серия побед: {stats.max_win_streak} · убытков: {stats.max_loss_streak}",
        "",
        f"Макс. просадка: −{fmt_num(stats.drawdown.max_drawdown)} USDT "
        f"({fmt_ratio(stats.drawdown.max_drawdown_percent)}%)",
    ]

    if stats.drawdown.current_drawdown > 0:
        lines.append(
            f"Текущая просадка: −{fmt_num(stats.drawdown.current_drawdown)} USDT"
        )

    return "\n".join(lines)


async def _render_period(
    session: AsyncSession, user: User, period: str
) -> str:
    offset = await _tz_offset(session, user.id)
    service = StatisticsService(session)
    start, end = period_bounds(period, tz_offset_hours=offset)
    snapshots = await service.load_snapshots(user.id, start=start, end=end)
    # Стартовый депозит нужен, чтобы процент просадки был осмысленным.
    equity = await service.starting_equity(user.id)
    stats = calculate_statistics(snapshots, starting_equity=equity)
    return render_statistics(stats, PERIOD_LABELS.get(period, "Статистика"))


async def _reply(event: Message | CallbackQuery, text: str, keyboard) -> None:  # type: ignore[no-untyped-def]
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Основная статистика
# ---------------------------------------------------------------------------


@router.callback_query(F.data == MenuCallback.STATS)
@router.message(Command("stats"))
async def show_statistics(
    event: Message | CallbackQuery, user: User, session: AsyncSession
) -> None:
    text = await _render_period(session, user, "all")

    unannotated = await StatisticsService(session).count_unannotated(user.id)
    if unannotated:
        text += (
            f"\n\n📌 {unannotated} импортированных сделок ждут разметки — "
            f"они не попадают в срезы по стратегиям и ошибкам."
        )

    await _reply(event, text, period_keyboard("all").as_markup())


@router.callback_query(F.data.startswith(StatsCB.PERIOD))
async def switch_period(
    callback: CallbackQuery, user: User, session: AsyncSession
) -> None:
    period = str(callback.data).removeprefix(StatsCB.PERIOD)
    text = await _render_period(session, user, period)
    await _reply(callback, text, period_keyboard(period).as_markup())


@router.message(Command("today"))
async def report_today(message: Message, user: User, session: AsyncSession) -> None:
    await _reply(
        message,
        await _render_period(session, user, "today"),
        period_keyboard("today").as_markup(),
    )


@router.message(Command("week"))
async def report_week(message: Message, user: User, session: AsyncSession) -> None:
    await _reply(
        message,
        await _render_period(session, user, "week"),
        period_keyboard("week").as_markup(),
    )


@router.message(Command("month"))
async def report_month(message: Message, user: User, session: AsyncSession) -> None:
    await _reply(
        message,
        await _render_period(session, user, "month"),
        period_keyboard("month").as_markup(),
    )


# ---------------------------------------------------------------------------
# Срезы
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(StatsCB.BREAKDOWN))
async def show_breakdown(
    callback: CallbackQuery, user: User, session: AsyncSession
) -> None:
    dimension = str(callback.data).removeprefix(StatsCB.BREAKDOWN)
    offset = await _tz_offset(session, user.id)
    service = StatisticsService(session)

    # Срез по стратегиям строится только на размеченных сделках: у
    # импортированных стратегия неизвестна, и они дали бы огромную
    # группу «не указано», из которой нельзя сделать вывод.
    snapshots = await service.load_snapshots(
        user.id, annotated_only=(dimension == "strategy")
    )

    if not snapshots:
        await _reply(
            callback, "Закрытых сделок пока нет.", period_keyboard().as_markup()
        )
        return

    if dimension == "time":
        by_hour = group_by_hour(snapshots, offset)[:6]
        by_weekday = group_by_weekday(snapshots, offset)
        lines = ["<b>Результат по времени</b>", "", "<b>Лучшие часы</b>"]
        lines += [
            f"{g.key} · {g.trades} сд. · {fmt_ratio(g.win_rate)}% · {fmt_money(g.total_pnl)}"
            for g in by_hour
        ]
        lines += ["", "<b>По дням недели</b>"]
        lines += [
            f"{g.key} · {g.trades} сд. · {fmt_ratio(g.win_rate)}% · {fmt_money(g.total_pnl)}"
            for g in by_weekday
        ]
        await _reply(callback, "\n".join(lines), period_keyboard().as_markup())
        return

    titles = {
        "symbol": "инструментам",
        "strategy": "стратегиям",
        "side": "направлениям",
    }
    groups = group_by(snapshots, dimension)

    lines = [f"<b>Результат по {titles.get(dimension, dimension)}</b>", ""]
    for group in groups[:15]:
        lines.append(
            f"<b>{group.key}</b>\n"
            f"Сделок: {group.trades} · Win Rate: {fmt_ratio(group.win_rate)}%\n"
            f"PnL: {fmt_money(group.total_pnl)} · средняя: {fmt_money(group.average_pnl)}\n"
        )

    await _reply(callback, "\n".join(lines), period_keyboard().as_markup())


# ---------------------------------------------------------------------------
# Просадка
# ---------------------------------------------------------------------------


@router.callback_query(F.data == MenuCallback.DRAWDOWN)
async def show_drawdown(
    callback: CallbackQuery, user: User, session: AsyncSession
) -> None:
    service = StatisticsService(session)
    snapshots = await service.load_snapshots(user.id)
    if not snapshots:
        await _reply(callback, "Закрытых сделок пока нет.", back_to_main())
        return

    equity = await service.starting_equity(user.id)
    stats = calculate_statistics(snapshots, starting_equity=equity)
    dd = stats.drawdown

    lines = [
        "<b>Просадка</b>",
        "",
        f"Максимальная: −{fmt_num(dd.max_drawdown)} USDT ({fmt_ratio(dd.max_drawdown_percent)}%)",
        f"Пик эквити: {fmt_num(dd.peak_equity)}",
        f"Дно: {fmt_num(dd.trough_equity)}",
        "",
    ]

    if dd.current_drawdown > 0:
        lines.append(f"Сейчас в просадке: −{fmt_num(dd.current_drawdown)} USDT")
    else:
        lines.append("Сейчас на максимуме эквити.")

    lines += [
        "",
        f"Максимальная серия убытков: {stats.max_loss_streak}",
        "",
        "<i>Просадка считается по кривой эквити закрытых сделок, "
        "peak-to-trough. Дневной лимит убытка — отдельная метрика.</i>",
    ]

    await _reply(callback, "\n".join(lines), back_to_main())


# ---------------------------------------------------------------------------
# Анализ ошибок
# ---------------------------------------------------------------------------


@router.callback_query(F.data == MenuCallback.MISTAKES)
@router.message(Command("mistakes"))
async def show_mistakes(
    event: Message | CallbackQuery, user: User, session: AsyncSession
) -> None:
    service = StatisticsService(session)
    impacts = await service.mistake_impact(user.id)

    if not impacts:
        await _reply(
            event,
            "Отмеченных ошибок пока нет.\n\n"
            "Ошибки проставляются при закрытии сделки или при разметке "
            "импортированных сделок. Без них анализ повторяющихся "
            "проблем невозможен.",
            back_to_main(),
        )
        return

    overall_avg = await service.overall_average_pnl(user.id)

    lines = ["<b>Анализ ошибок</b>", ""]

    worst = impacts[0]
    if worst.total_pnl < 0:
        lines += [
            f"⚠️ <b>Самая дорогая: {worst.title}</b>",
            f"{worst.occurrences} {plural_trades(worst.occurrences)} · "
            f"Win Rate {fmt_ratio(worst.win_rate)}%",
            f"Суммарно: {fmt_money(worst.total_pnl)} USDT",
            "",
        ]

    for impact in impacts:
        line = (
            f"<b>{impact.title}</b> — {impact.occurrences} сд. · "
            f"{fmt_ratio(impact.win_rate)}% · {fmt_money(impact.total_pnl)} USDT"
        )
        # Сравнение со средней сделкой: цифра по ошибке сама по себе
        # ни о чём не говорит без базы для сравнения.
        if overall_avg is not None and impact.average_pnl < overall_avg:
            delta = overall_avg - impact.average_pnl
            line += f"\n<i>Хуже средней сделки на {fmt_num(delta)} USDT</i>"
        lines.append(line)
        lines.append("")

    if overall_avg is not None:
        lines.append(f"<i>Средняя сделка вообще: {fmt_money(overall_avg)} USDT</i>")

    await _reply(event, "\n".join(lines), back_to_main())


@router.callback_query(F.data == MenuCallback.REPORT)
async def show_report(
    callback: CallbackQuery, user: User, session: AsyncSession
) -> None:
    """Сводный отчёт: три периода в одном сообщении."""
    parts = []
    for period in ("today", "week", "month"):
        offset = await _tz_offset(session, user.id)
        start, end = period_bounds(period, tz_offset_hours=offset)
        service = StatisticsService(session)
        snapshots = await service.load_snapshots(user.id, start=start, end=end)
        stats = calculate_statistics(
            snapshots, starting_equity=await service.starting_equity(user.id)
        )
        label = PERIOD_LABELS[period]

        if stats.total_trades == 0:
            parts.append(f"<b>{label}</b>\nСделок нет.")
            continue

        parts.append(
            f"<b>{label}</b>\n"
            f"Сделок: {stats.total_trades} · Win Rate: {fmt_ratio(stats.win_rate)}%\n"
            f"PnL: {fmt_money(stats.total_pnl)} USDT\n"
            f"Макс. DD: −{fmt_num(stats.drawdown.max_drawdown)}"
        )

    await _reply(callback, "\n\n".join(parts), back_to_main())
