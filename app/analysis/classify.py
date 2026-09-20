"""Классификация результата детекторов: READY / FORMING / ничего.

Вынесено из app/workers/scanner.py, чтобы экран «Анализ рынка» и сканер
опирались на одно определение, а не на две копии, которые рано или поздно
разойдутся. Сканер по-прежнему импортирует имена отсюда (и реэкспортирует
их — на них ссылаются тесты).

READY — Signal.is_actionable (все условия детектора выполнены). FORMING —
единственное невыполненное условие во всём Signal.conditions это
"Подтверждающий паттерн" (см. app/analysis/setups.py: оба детектора
проверяют его последним, прямо перед расчётом входа). Это единственное
место, завязанное на конкретные имена условий детекторов — если в
setups.py появится новый детектор с другим порядком проверок, это тоже
нужно будет учесть здесь.
"""

from __future__ import annotations

from app.analysis.signals import Signal
from app.trading.enums import SignalLevel, Timeframe

CONFIRMATION_CONDITION_NAME = "Подтверждающий паттерн"

# Таймфреймы, по которым ходит сканер. Экран анализа считает вердикт по
# тем же двум — иначе сетап, о котором сканер уже написал, на экране
# выглядел бы как «не найден».
SCAN_TIMEFRAMES = (Timeframe.H1.value, Timeframe.H4.value)


def classify_signal(signal: Signal) -> SignalLevel | None:
    """READY/FORMING/ни один — см. docstring модуля."""
    if signal.is_actionable:
        return SignalLevel.READY

    failed = signal.failed_conditions
    if len(failed) == 1 and failed[0].name == CONFIRMATION_CONDITION_NAME:
        return SignalLevel.FORMING
    return None
