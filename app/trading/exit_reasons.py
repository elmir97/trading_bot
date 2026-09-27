"""Причины выхода, которые пишет бот (reconciler, шаг 15.6).

Trade.exit_reason — свободный текст: пользователь пишет туда своё при ручном
закрытии, и он показывается на карточке сделки. Тексты бота — только
отсюда, не строками по коду: по ним же отчёт по демо-периоду считает
закрытия по стопу, тейку и вне бота.
"""

from __future__ import annotations

EXIT_STOP_LOSS = "Стоп-лосс на бирже"
EXIT_TAKE_PROFIT = "Тейк-профит на бирже"
EXIT_OUTSIDE_BOT = "Закрыта на бирже вне бота"

BOT_EXIT_REASONS = frozenset({EXIT_STOP_LOSS, EXIT_TAKE_PROFIT, EXIT_OUTSIDE_BOT})
