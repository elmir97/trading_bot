"""Ответы API. Деньги, цены, проценты — Decimal: в JSON уходят строками
(pydantic v2). Обычно округлены для показа; fee_usd сохраняет точность, а fee_text
отвечает за показ комиссии. JS только добавляет подписи и разряды (§5)."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CENT = Decimal("0.01")


def money(value: Decimal | None) -> Decimal | None:
    """Деньги и проценты для показа — два знака, ROUND_HALF_UP (как formatting.py)."""
    return None if value is None else value.quantize(CENT, rounding=ROUND_HALF_UP)


def price(value: Decimal | None, precision: int) -> Decimal | None:
    """Цена — знаков по pricePrecision контракта (как fmt_price)."""
    if value is None:
        return None
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


class ErrorBody(BaseModel):
    code: str
    message: str


class Health(BaseModel):
    ok: bool


class Plan(BaseModel):
    risk_percent: Decimal | None      # риск по умолчанию = максимум плана
    max_leverage: int | None
    min_rr: Decimal | None
    allowed_symbols: list[str]


class Flags(BaseModel):
    dry_run: bool                     # EXEC_OPEN_DRY_RUN — на биржу не уйдёт
    live_allowed: bool                # EXEC_OPEN_ALLOW_LIVE


class ActionsFlags(BaseModel):
    """Действия с позицией (экран 5)."""

    enabled: bool                     # MINIAPP_ACTIONS_ENABLED — иначе кнопки ведут в чат
    dry_run: bool                     # EXEC_DRY_RUN — действие на биржу не уйдёт
    card_ttl_seconds: int             # exec_confirm_ttl_seconds


class Me(BaseModel):
    account_mode: str                 # DEMO | LIVE
    equity: Decimal | None
    available: Decimal | None
    balance_error: str | None         # баланс не прочитан — почему (экран: «—»)
    margin_type: str                  # ISOLATED | CROSSED — для новых сделок
    plan: Plan
    flags: Flags
    limit_expiry_minutes: list[int]
    limit_expiry_default: int
    card_ttl_seconds: int
    # Имя бота для ссылки «⚙️ Действия в чате» (getMe при старте API); не прочитано — None,
    # кнопки нет.
    bot_username: str | None = None
    actions: ActionsFlags | None = None   # действия с позицией (экран 5)


class Contract(BaseModel):
    symbol: str
    price_precision: int
    quantity_precision: int
    min_quantity: Decimal
    min_notional: Decimal
    # Максимум плеча контракта для счёта (GET trade/leverage по ключу, кэш 5 мин): нет
    # ключа, сбой чтения или в /api/symbols ещё не читали — None, плашку N× не показывать.
    max_leverage: int | None = None


class Change(BaseModel):
    value: Decimal | None
    percent: Decimal | None


class Leverage(BaseModel):
    long: int
    short: int
    max_long: int
    max_short: int


class Market(BaseModel):
    contract: Contract
    last: Decimal
    time: datetime                    # время котировки (UTC)
    interval: str
    candle_change: Change             # last против open текущей свечи интервала
    day_change: Change                # за 24 ч (тикер)
    quote_volume: Decimal | None      # объём за 24 ч, USDT
    open_interest: Decimal | None     # USDT
    funding_rate_percent: Decimal | None
    next_funding_time: datetime | None
    leverage: Leverage | None         # счёта по символу; нет ключа/сбой — None
    leverage_error: str | None
    details_error: str | None         # фандинг/ОИ не прочитаны — экран «—»


class Candle(BaseModel):
    time: int                         # unix, секунды — открытие свечи
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class Candles(BaseModel):
    symbol: str
    interval: str
    candles: list[Candle]


# --- открытие (§3, §5а) ------------------------------------------------------------

# Ввод — строкой или числом; NaN/Infinity и мусор длиной в килобайт — 422.
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=30)]


class LevelIn(BaseModel):
    kind: Literal["price", "percent"]   # percent — расстояние от входа, > 0
    value: Positive


class RiskIn(BaseModel):
    kind: Literal["usd", "percent"]
    value: Positive


class OpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(pattern=r"^[A-Z0-9]{1,24}-USDT$")
    side: Literal["LONG", "SHORT"]
    entry_type: Literal["MARKET", "LIMIT"]
    stop: LevelIn
    take: LevelIn | None = None         # без тейка — null
    risk: RiskIn
    leverage: int = Field(ge=1, le=500)
    limit_price: Positive | None = None
    expiry_minutes: int | None = None   # только лимит; нет — по умолчанию настроек


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept_warnings: bool = False       # «Открыть всё равно»


class IssueOut(BaseModel):
    code: str
    level: str                          # BLOCK | WARN
    message: str


class RefusalOut(BaseModel):
    code: str
    message: str


class RoundedOut(BaseModel):
    field: str                          # stop_loss | take_profit | limit_price
    label: str
    before: Decimal
    after: Decimal


class LevelsOut(BaseModel):
    """Ввод после перевода % / $ и шага цены — то, что посчитано и уйдёт."""

    entry_type: str
    stop_loss: Decimal
    take_profit: Decimal | None
    limit_price: Decimal | None
    risk_percent: Decimal               # 4 знака, как в trade_openings
    leverage: int
    expiry_minutes: int | None


class CalcOut(BaseModel):
    entry_price: Decimal
    quantity: Decimal
    notional: Decimal
    risk_usd: Decimal                   # с комиссией входа и выхода по стопу
    risk_percent: Decimal
    fee_usd: Decimal                   # exact amount; fee_text is presentation only
    fee_text: str | None = None         # additive; older responses may omit it
    margin: Decimal
    liq_estimate: Decimal | None
    liq_ratio: Decimal | None
    distance_percent: Decimal | None
    rr: Decimal | None
    max_leverage_for_stop: int
    suggested_leverage: int
    take_2r: Decimal | None             # тейк на 2R от входа и стопа — чип «2R» формы
    fills_immediately: bool


class CardOut(BaseModel):
    text: str                           # карточка как в чате (render_card) или отказ
    can_open: bool
    has_warnings: bool
    issues: list[IssueOut]
    refusal: RefusalOut | None
    calc: CalcOut | None
    levels: LevelsOut | None
    rounded: list[RoundedOut]
    price_precision: int | None
    quantity_precision: int | None


class OpenOut(BaseModel):
    opening_id: int | None              # None — отказ до расчёта уровней, строки нет
    status: str | None                  # CARD | REFUSED
    expires_at: datetime | None         # карточка действует до (exec_open_card_ttl_seconds)
    card: CardOut


class OpeningOut(BaseModel):
    id: int
    status: str
    final: bool                         # дальше статус сам не изменится
    text: str
    symbol: str
    side: str
    entry_type: str
    quantity: Decimal | None            # исполнено, иначе по карточке
    avg_price: Decimal | None
    limit_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    liq_estimate: Decimal | None
    stop_placed: bool                   # стоп стоит на бирже (строка SUBMITTED)
    take_placed: bool
    trade_id: int | None
    expires_at: datetime | None         # CARD — срок карточки, WORKING — срок лимита
    error_code: str | None
    error_message: str | None


class ActionOut(BaseModel):
    """Итог confirm / cancel: text — ответ ядра; pending — ещё идёт, опрашивать GET."""

    text: str
    final: bool
    pending: bool = False
    busy: bool = False                  # лок позиции занят — повторить через пару секунд
    opening: OpeningOut


def qty(value: Decimal | None, precision: int) -> Decimal | None:
    """Объём — знаков по quantityPrecision (он уже на шаге лота)."""
    return price(value, precision)


# --- «Позиции» (П.1) -------------------------------------------------------------------


class ProtectiveOut(BaseModel):
    """Стоп или тейк на бирже. close_position — на всю позицию (quantity нет)."""

    price: Decimal
    close_position: bool
    quantity: Decimal | None


class BarOut(BaseModel):
    """Полоска стоп — вход — тейк (Д.6): шкала от ближнего стопа (0) к ближнему тейку (1),
    у шорта зеркальная (стоп выше тейка) — считает сервер; фронт только ставит left:%."""

    stop: str                           # «стоп 1.4402» — подписи с точностью контракта
    entry: str
    take: str
    mark: str                           # цена (mark) текстом
    entry_at: Decimal                   # 0..1, обрезана по краям
    mark_at: Decimal                    # 0..1, обрезана по краям
    mark_edge: str | None               # stop | take — цена за этим краем (обрезана)


class PositionOut(BaseModel):
    symbol: str
    side: str                           # LONG | SHORT
    quantity: Decimal
    leverage: int
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal | None
    pnl_text: str | None                # «+2.73 $» / «-0.85 $» / «0.00 $» — знак ставит сервер
    pnl_sign: int                       # 1 / -1 / 0 (нет PnL — 0): цвет на фронте
    liquidation_price: Decimal | None
    price_precision: int | None         # контракта нет в списке — цены без округления
    bar: BarOut | None                  # нет стопа или тейка (или они не по сторонам) — нет
    stops: list[ProtectiveOut]          # от ближнего к цене; несколько — лестница
    takes: list[ProtectiveOut]
    trade_id: int | None                # сделка журнала того же счёта; None — «не в журнале»
    start_param: str                    # t.me/<бот>?start=… — экран действий в чате


class JournalTradeOut(BaseModel):
    """Открытая сделка только в журнале (без позиции на бирже), любого счёта."""

    id: int
    symbol: str
    side: str
    entry_price: Decimal | None         # по точности контракта; нет в списке — без хвоста нулей
    account_mode: str | None            # DEMO | LIVE | None — ручная запись без счёта


class PositionsOut(BaseModel):
    account_mode: str                   # счёт Mini App (сервер)
    bot_account_mode: str               # выбранный в боте — «Позиции» в чате покажут его
    positions: list[PositionOut]
    journal_only: list[JournalTradeOut]
    exchange_error: ErrorBody | None    # NO_KEY / KEY_REJECTED — показан только журнал


# --- действия с позицией (экран 5, 5.1) ------------------------------------------------

# be — стоп в безубыток, sl — перенос стопа, tp — тейк, c25/c50 — закрыть 25/50 %,
# cf — закрыть всё (те же коды, что у кнопок чата pa:<код>:…).
ActionCode = Literal["be", "sl", "tp", "c25", "c50", "cf"]
LevelText = Annotated[str, Field(min_length=1, max_length=32)]


class ActionIn(BaseModel):
    """Цена (level) или отступ от mark в % (level_percent, чип стопа) — строкой, только
    для sl/tp и ровно одно из двух; у тейка отступа нет. Перевод и проверка — ядро."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(pattern=r"^[A-Z0-9]{1,24}-USDT$")
    side: Literal["LONG", "SHORT"]
    kind: ActionCode
    level: LevelText | None = None
    level_percent: LevelText | None = None

    @model_validator(mode="after")
    def _levels(self) -> ActionIn:
        given = [v for v in (self.level, self.level_percent) if v is not None]
        if self.kind in ("sl", "tp"):
            if len(given) != 1:
                raise ValueError("нужна цена или отступ — одно из двух")
            if self.kind == "tp" and self.level_percent is not None:
                raise ValueError("у тейка отступа в % нет")
        elif given:
            raise ValueError("цена — только у стопа и тейка")
        return self


class ActionConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_confirmed: bool = False        # «⚠️ Да, увеличить риск» (= card.risk_increase)


class ActionRowOut(BaseModel):
    label: str
    value: str                          # строка карточки чата (card_view), как есть


class ActionCardOut(BaseModel):
    """Карточка действия: строки — те же, что в чате (render_card из card_view)."""

    text: str                           # карточка как в чате или «⛔ причина»
    refusal: RefusalOut | None
    title: str | None                   # «XRP-USDT LONG · перенос стопа»; отказ — None
    rows: list[ActionRowOut]
    notes: list[str]
    risk_increase: bool
    new_level: str | None               # стоп/тейк после действия — пунктир на графике


class ActionHintOut(BaseModel):
    code: str                           # be | sl | tp | c25 | c50 | cf
    enabled: bool                       # подсказка; решает карточка
    hint: str | None                    # почему нельзя (отказ ядра)


class PositionActionsOut(BaseModel):
    """Экран действий позиции: позиция и план каждого вида на одних данных (без записи)."""

    position: PositionOut | None        # отказ до чтения позиции — None, причина — refusal
    refusal: RefusalOut | None
    # «+0.71R»; сделка без 1R — «R не определён»; сделки в журнале нет — None
    pnl_r_text: str | None
    breakeven_price: Decimal | None
    actions: list[ActionHintOut]


class ActionOpenOut(BaseModel):
    action_id: int | None               # REFUSED — None (строка отказа есть, «Да» по ней нет)
    status: str                         # CARD | REFUSED
    expires_at: datetime | None         # created_at + exec_confirm_ttl_seconds
    ttl_seconds: int | None
    card: ActionCardOut


class ActionStatusOut(BaseModel):
    id: int
    code: str                           # be | sl | tp | c25 | c50 | cf
    kind: str                           # MOVE_STOP | SET_TAKE | CLOSE_PARTIAL | CLOSE_FULL
    symbol: str
    side: str
    status: str
    final: bool                         # дальше статус сам не изменится (не CARD, не SUBMITTED)
    text: str                           # итог в чате (без тегов) или по статусу
    error_code: str | None
    expires_at: datetime | None         # только CARD
    decided_at: datetime | None


class ActionResultOut(BaseModel):
    """Итог «Да»/«Нет»: pending — идёт, опрашивать GET; busy — лок занят, повторить."""

    text: str
    final: bool
    pending: bool = False
    busy: bool = False
    action: ActionStatusOut


__all__ = [
    "ActionCardOut",
    "ActionConfirmIn",
    "ActionHintOut",
    "ActionIn",
    "ActionOpenOut",
    "ActionOut",
    "ActionResultOut",
    "ActionRowOut",
    "ActionStatusOut",
    "ActionsFlags",
    "CalcOut",
    "Candle",
    "Candles",
    "CardOut",
    "Change",
    "ConfirmIn",
    "Contract",
    "ErrorBody",
    "Flags",
    "Health",
    "IssueOut",
    "JournalTradeOut",
    "LevelIn",
    "LevelsOut",
    "Leverage",
    "Market",
    "Me",
    "OpenIn",
    "OpenOut",
    "OpeningOut",
    "Plan",
    "PositionActionsOut",
    "PositionOut",
    "PositionsOut",
    "ProtectiveOut",
    "RefusalOut",
    "RiskIn",
    "RoundedOut",
    "money",
    "price",
    "qty",
]
