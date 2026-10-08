"""Реэкспорт всех моделей.

Alembic импортирует именно этот модуль: любая модель, не попавшая сюда,
не окажется в metadata и будет молча пропущена при автогенерации миграции.
"""

from app.database.base import Base
from app.database.models.ai_report import AIReport
from app.database.models.credentials import ExchangeCredentials
from app.database.models.execution_callback import ExecutionCallback
from app.database.models.execution_order import ExecutionOrder
from app.database.models.mistake import SYSTEM_MISTAKES, MistakeType, TradeMistake
from app.database.models.outgoing_message import OutgoingMessage
from app.database.models.position_action import PositionAction
from app.database.models.position_alert import PositionAlert
from app.database.models.reconciliation_event import ReconciliationEvent
from app.database.models.signal import SignalRecord
from app.database.models.signal_notification import SignalNotification
from app.database.models.strategy import DEFAULT_STRATEGIES, Strategy
from app.database.models.trade import Trade, TradeFill
from app.database.models.trade_opening import TradeOpening
from app.database.models.trading_plan import (
    DEFAULT_ALLOWED_SYMBOLS,
    DEFAULT_ALLOWED_TIMEFRAMES,
    TradingPlan,
)
from app.database.models.user import DEFAULT_NOTIFICATIONS, User, UserSettings

__all__ = [
    "DEFAULT_ALLOWED_SYMBOLS",
    "DEFAULT_ALLOWED_TIMEFRAMES",
    "DEFAULT_NOTIFICATIONS",
    "DEFAULT_STRATEGIES",
    "SYSTEM_MISTAKES",
    "AIReport",
    "Base",
    "ExchangeCredentials",
    "ExecutionCallback",
    "ExecutionOrder",
    "MistakeType",
    "OutgoingMessage",
    "PositionAction",
    "PositionAlert",
    "ReconciliationEvent",
    "SignalNotification",
    "SignalRecord",
    "Strategy",
    "Trade",
    "TradeFill",
    "TradeOpening",
    "TradeMistake",
    "TradingPlan",
    "User",
    "UserSettings",
]
