"""Состояния диалогов.

Каждая форма — отдельная группа состояний. Общий FSM на всё привёл бы к
пересечению обработчиков текста: бот не понимал бы, вводят ли сейчас цену
входа или причину выхода.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddTradeStates(StatesGroup):
    """Добавление сделки вручную.

    Порядок полей повторяет порядок принятия решения трейдером: сначала
    инструмент и направление, затем цены, затем стоп и цель, и только
    потом разметка. Так форма ощущается как проговаривание сделки.
    """

    symbol = State()
    side = State()
    entry_price = State()
    stop_loss = State()
    take_profit = State()
    quantity_mode = State()   # ввести объём или рассчитать от риска
    quantity = State()
    leverage = State()
    strategy = State()
    timeframe = State()
    entry_reason = State()
    confirm = State()


class CloseTradeStates(StatesGroup):
    select_trade = State()
    exit_price = State()
    fee = State()
    exit_reason = State()
    # Разметка ошибок идёт последним шагом, уже после сохранения результата:
    # так закрытие не потеряется, если пользователь бросит диалог здесь.
    mistakes = State()


class AnnotateTradeStates(StatesGroup):
    """Разметка импортированной сделки."""

    select_trade = State()
    strategy = State()
    mistakes = State()
    notes = State()


class RiskCalculatorStates(StatesGroup):
    balance = State()
    risk_percent = State()
    entry_price = State()
    stop_loss = State()
    take_profit = State()


class SettingsStates(StatesGroup):
    risk_per_trade = State()
    max_daily_loss = State()
    max_trades_per_day = State()
    api_key = State()
    api_secret = State()
