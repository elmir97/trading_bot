"""Фейковая биржа для открытия сделки из бота — поведение BingX по разведке
Р1–Р8 (05.10.2026, docs/handoff.md):

- стоп/тейк вложены во вход; неверный стоп относительно last — отказ всего
  ордера 101400 (Р4); после исполнения — отдельные STOP_MARKET/TAKE_PROFIT_MARKET
  с пустым clientOrderId, reduceOnly, positionID позиции (Р3/Р6);
- лимит до исполнения — PENDING, SL/TP внутри ордера (Р5); лимит, пересекающий
  рынок, исполняется сразу (Р6); отмена по clientOrderId — CANCELLED;
- режим маржи не меняется при позиции/ордерах по символу — 104103 (Р3а);
- плечо принимается любое, read-back — get_leverage (Р2);
- clientOrderId хранится в нижнем регистре; GET несуществующего — 109421.

Сбои включаются флагами (см. атрибуты в __init__)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.exchanges.base import (
    Balance,
    CancelResult,
    CommissionRate,
    ExchangeResponseError,
    ExchangeUnavailableError,
    LeverageInfo,
    MarginType,
    OpenOrder,
    OrderFill,
    OrderNotFoundError,
    OrderResult,
    Position,
    SymbolInfo,
    Ticker,
    TpSlSpec,
)
from app.trading.enums import OrderSide, TradeSide

D = Decimal
SYMBOL = "XRP-USDT"


def _now() -> datetime:
    return datetime.now(UTC)


class OpeningExchange:
    name = "bingx"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.last = D("1.4950")
        self.mark = D("1.4950")
        self.equity = D(1500)
        self.available: Decimal | None = D(1475)
        self.taker = D("0.0005")
        self.margin_type = MarginType.ISOLATED
        self.leverage = {"LONG": 20, "SHORT": 20}
        self.max_leverage = 125
        self.mmr = D("0.0041")
        self.info = SymbolInfo(SYMBOL, 4, 0, D(2), D(2))
        self.positions: dict[str, Position] = {}
        self.orders: list[OpenOrder] = []          # открытые ордера (как openOrders)
        self.by_cid: dict[str, dict[str, Any]] = {}  # все ордера с cid (для GET)
        self._seq = 2107135113934413824
        self._pid = 2107135113963773954
        # --- сбои ---
        self.reject_entry_code: int | None = None   # отказ входа кодом
        self.timeout_entry = False                  # таймаут на POST входа...
        self.timeout_after_accept = True            # ...после того как ордер принят
        self.drop_attached_sl = False               # вложенный стоп не появился
        self.drop_attached_tp = False
        self.fail_conditional = False               # отдельный стоп отклоняется
        self.fail_close = False                     # аварийное закрытие отклоняется
        self.leverage_readback_off = False           # POST плеча «принят», но не применён
        self.margin_readback_off = False
        self.fail_reads: set[str] = set()           # имена GET, падающих ExchangeUnavailableError
        self.liquidation_override: Decimal | None = None

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def posts(self) -> list[tuple[str, Any]]:
        return [c for c in self.calls if c[0].startswith("post_")]

    def _read(self, name: str, payload: Any = None) -> None:
        self.calls.append((name, payload))
        if name in self.fail_reads:
            raise ExchangeUnavailableError("BingX не ответил вовремя")

    def _next_id(self) -> str:
        self._seq += 1
        return str(self._seq)

    # --- чтение -----------------------------------------------------------------

    async def get_symbols(self, *, max_retries: int | None = None) -> list[SymbolInfo]:
        self._read("symbols")
        return [self.info]

    async def get_ticker(self, symbol: str, *, max_retries: int | None = None) -> Ticker:
        self._read("ticker", symbol)
        return Ticker(symbol, self.last, _now())

    async def get_mark_price(self, symbol: str) -> Decimal:
        self._read("mark", symbol)
        return self.mark

    async def get_balance(self, *, max_retries: int | None = None) -> Balance:
        self._read("balance")
        return Balance("VST", self.available, D(0), D(0), self.equity)

    async def get_leverage(self, symbol: str, *, max_retries: int | None = None) -> LeverageInfo:
        self._read("leverage", symbol)
        return LeverageInfo(
            symbol, self.leverage["LONG"], self.leverage["SHORT"],
            self.max_leverage, self.max_leverage,
        )

    async def get_margin_type(self, symbol: str, *, max_retries: int | None = None) -> MarginType:
        self._read("margin_type", symbol)
        return self.margin_type

    async def get_position_mode(self, *, max_retries: int | None = None) -> bool:
        self._read("position_mode")
        return True

    async def get_positions(self, *, max_retries: int | None = None) -> list[Position]:
        self._read("positions")
        return list(self.positions.values())

    async def get_open_orders(
        self, symbol: str | None = None, *, max_retries: int | None = None
    ) -> list[OpenOrder]:
        self._read("open_orders", symbol)
        return list(self.orders)

    async def get_commission_rate(self) -> CommissionRate:
        self._read("commission")
        return CommissionRate(taker=self.taker, maker=D("0.0002"))

    async def get_order_fill(
        self, symbol: str, client_order_id: str, *, max_retries: int | None = None
    ) -> OrderFill:
        self._read("order_fill", client_order_id)
        order = self.by_cid.get(client_order_id.lower())
        if order is None:
            raise ExchangeResponseError("BingX: order not exist (код 109421)", code=109421)
        return OrderFill(
            order_id=order["order_id"], client_order_id=client_order_id.lower(),
            status=order["status"], avg_price=order["avg"], orig_qty=order["qty"],
            executed_qty=order["executed"], fee=order["fee"], raw={},
            filled_at=order.get("filled_at"), position_id=order.get("position_id"),
        )

    # --- запись -----------------------------------------------------------------

    async def set_margin_type(self, symbol: str, margin_type: MarginType) -> MarginType:
        self.calls.append(("post_margin_type", margin_type))
        if any(p.symbol == symbol for p in self.positions.values()) or self.orders:
            raise ExchangeResponseError(
                "BingX: Please close open positions or cancel pending orders first. (код 104103)",
                code=104103,
            )
        if not self.margin_readback_off:
            self.margin_type = margin_type
        return margin_type

    async def set_leverage(
        self, symbol: str, leverage: int, *, position_side: str | None = None
    ) -> int:
        self.calls.append(("post_leverage", (position_side, leverage)))
        if not self.leverage_readback_off:
            self.leverage[position_side or "LONG"] = leverage
        return leverage

    def _check_levels(
        self, side: OrderSide, stop: TpSlSpec | None, take: TpSlSpec | None
    ) -> None:
        sign = 1 if side is OrderSide.BUY else -1
        if stop is not None and (self.last - stop.trigger_price) * sign <= 0:
            word = "lower" if sign > 0 else "higher"
            raise ExchangeResponseError(
                f"BingX: SL Price must be {word} than Last Price (код 101400)", code=101400
            )
        if take is not None and (take.trigger_price - self.last) * sign <= 0:
            word = "higher" if sign > 0 else "lower"
            raise ExchangeResponseError(
                f"BingX: TP Price must be {word} than Last Price (код 101400)", code=101400
            )

    def _accept(self, kind: str, kw: dict[str, Any]) -> None:
        if self.reject_entry_code is not None:
            raise ExchangeResponseError(
                f"BingX: rejected (код {self.reject_entry_code})", code=self.reject_entry_code
            )
        self._check_levels(kw["side"], kw.get("stop_loss"), kw.get("take_profit"))

    async def place_market_order(self, **kw: Any) -> OrderResult:
        self.calls.append(("post_market", kw))
        cid = kw["client_order_id"].lower()
        position_side = kw["position_side"]
        opening = (kw["side"] is OrderSide.BUY) == (position_side == "LONG")
        if opening:
            if self.timeout_entry and not self.timeout_after_accept:
                raise ExchangeUnavailableError("BingX не ответил вовремя")
            self._accept("market", kw)
        elif self.fail_close:
            raise ExchangeResponseError("BingX: close rejected (код 101204)", code=101204)
        order_id = self._next_id()
        self._fill(cid, order_id, kw, kw["quantity"], self.last, opening=opening)
        if opening and self.timeout_entry:
            raise ExchangeUnavailableError("BingX не ответил вовремя")
        return OrderResult(order_id, cid, SYMBOL, kw["side"].value, position_side,
                           "MARKET", "FILLED", {})

    async def place_limit_order(self, **kw: Any) -> OrderResult:
        self.calls.append(("post_limit", kw))
        cid = kw["client_order_id"].lower()
        if self.timeout_entry and not self.timeout_after_accept:
            raise ExchangeUnavailableError("BingX не ответил вовремя")
        self._accept("limit", kw)
        order_id = self._next_id()
        sign = 1 if kw["side"] is OrderSide.BUY else -1
        if (kw["price"] - self.last) * sign >= 0:   # Р6: исполняется сразу по рынку
            self._fill(cid, order_id, kw, kw["quantity"], self.last, opening=True)
            status = "FILLED"
        else:
            self.by_cid[cid] = {
                "order_id": order_id, "status": "PENDING", "avg": D(0), "qty": kw["quantity"],
                "executed": D(0), "fee": D(0), "kw": kw, "position_id": None,
            }
            self.orders.append(self._open(order_id, cid, kw, "LIMIT", kw["price"], None))
            status = "PENDING"
        if self.timeout_entry:
            raise ExchangeUnavailableError("BingX не ответил вовремя")
        return OrderResult(order_id, cid, SYMBOL, kw["side"].value, kw["position_side"],
                           "LIMIT", status, {})

    def fill_limit(
        self, cid: str, qty: Decimal | None = None, price: Decimal | None = None
    ) -> None:
        """Цена дошла до лимита: исполнить qty (по умолчанию остаток)."""
        order = self.by_cid[cid.lower()]
        kw = order["kw"]
        rest = order["qty"] - order["executed"]
        take = rest if qty is None else min(qty, rest)
        self._fill(cid.lower(), order["order_id"], kw, take, price or kw["price"], opening=True,
                   partial_of=order)

    async def cancel_order_by_client_id(self, symbol: str, client_order_id: str) -> CancelResult:
        self.calls.append(("post_cancel", client_order_id))
        order = self.by_cid.get(client_order_id.lower())
        if order is None or order["status"] not in ("PENDING", "PARTIALLY_FILLED"):
            raise OrderNotFoundError("BingX: order not exist (код 109400)", code=109400)
        order["status"] = "CANCELLED"
        self.orders = [o for o in self.orders if o.order_id != order["order_id"]]
        return CancelResult(order["order_id"], "CANCELLED", {})

    async def cancel_order(self, symbol: str, order_id: str) -> CancelResult:
        self.calls.append(("post_cancel_id", order_id))
        before = len(self.orders)
        self.orders = [o for o in self.orders if o.order_id != order_id]
        if len(self.orders) == before:
            raise OrderNotFoundError("BingX: order not exist (код 109400)", code=109400)
        return CancelResult(order_id, "CANCELLED", {})

    async def place_conditional_order(self, **kw: Any) -> OrderResult:
        self.calls.append(("post_conditional", kw))
        if self.fail_conditional:
            raise ExchangeResponseError("BingX: rejected (код 109400)", code=109400)
        position = self.positions.get(kw["position_side"])
        order_id = self._next_id()
        self.orders.append(OpenOrder(
            order_id=order_id, client_order_id=kw["client_order_id"].lower(), symbol=SYMBOL,
            side=kw["side"].value, position_side=kw["position_side"],
            order_type=kw["order_type"], quantity=kw["quantity"], executed_qty=D(0),
            price=D(0), stop_price=kw["stop_price"], status="NEW", leverage=20,
            reduce_only=True, close_position=kw.get("close_position", True),
            working_type="MARK_PRICE", created_at=_now(), updated_at=_now(),
            take_profit=None, stop_loss=None,
            position_id=position.position_id if position else None,
        ))
        return OrderResult(order_id, kw["client_order_id"].lower(), SYMBOL, kw["side"].value,
                           kw["position_side"], kw["order_type"], "NEW", {})

    async def close(self) -> None: ...

    # --- внутреннее --------------------------------------------------------------

    def _open(self, order_id: str, cid: str, kw: dict[str, Any], otype: str,
              price: Decimal, stop: Decimal | None, *, position_id: str | None = None,
              qty: Decimal | None = None) -> OpenOrder:
        return OpenOrder(
            order_id=order_id, client_order_id=cid, symbol=SYMBOL, side=kw["side"].value
            if otype == "LIMIT" else ("SELL" if kw["position_side"] == "LONG" else "BUY"),
            position_side=kw["position_side"], order_type=otype,
            quantity=qty if qty is not None else kw["quantity"], executed_qty=D(0), price=price,
            stop_price=stop, status="PENDING" if otype == "LIMIT" else "NEW", leverage=20,
            reduce_only=otype != "LIMIT", close_position=False, working_type="MARK_PRICE",
            created_at=_now(), updated_at=_now(), take_profit=None, stop_loss=None,
            position_id=position_id,
        )

    def _liq(self, side: str, entry: Decimal) -> Decimal:
        if self.liquidation_override is not None:
            return self.liquidation_override
        inv = D(1) / D(self.leverage[side])
        return entry * (1 - inv + self.mmr) if side == "LONG" else entry * (1 + inv - self.mmr)

    def _fill(self, cid: str, order_id: str, kw: dict[str, Any], qty: Decimal,
              price: Decimal, *, opening: bool, partial_of: dict[str, Any] | None = None) -> None:
        side = kw["position_side"]
        fee = qty * price * self.taker
        current = self.positions.get(side)
        if opening:
            if current is None:
                self._pid += 1
                pos = Position(SYMBOL, TradeSide(side), qty, price, self.mark,
                               self.leverage[side], D(0), position_id=str(self._pid))
            else:
                total = current.quantity + qty
                avg = (current.entry_price * current.quantity + price * qty) / total
                pos = replace(current, quantity=total, entry_price=avg)
            self.positions[side] = replace(pos, liquidation_price=self._liq(side, pos.entry_price))
            pid = self.positions[side].position_id
            # Р3/Р6: вложенные SL/TP — отдельными ордерами на исполненный объём.
            stop: TpSlSpec | None = kw.get("stop_loss")
            take: TpSlSpec | None = kw.get("take_profit")
            if stop is not None and not self.drop_attached_sl:
                self.orders.append(self._open(self._next_id(), "", kw, "STOP_MARKET", D(0),
                                              stop.trigger_price, position_id=pid, qty=qty))
            if take is not None and not self.drop_attached_tp:
                self.orders.append(self._open(self._next_id(), "", kw, "TAKE_PROFIT_MARKET",
                                              D(0), take.trigger_price, position_id=pid, qty=qty))
        else:
            assert current is not None, "закрытие без позиции"
            left = current.quantity - qty
            if left <= 0:
                del self.positions[side]
                pid = current.position_id
                self.orders = [o for o in self.orders if o.position_id != pid]
            else:
                self.positions[side] = replace(current, quantity=left)
            pid = current.position_id
        if partial_of is not None:
            partial_of["executed"] += qty
            partial_of["avg"] = price
            partial_of["fee"] += fee
            partial_of["position_id"] = pid
            partial_of["filled_at"] = _now()
            done = partial_of["executed"] >= partial_of["qty"]
            partial_of["status"] = "FILLED" if done else "PARTIALLY_FILLED"
            if done:
                self.orders = [o for o in self.orders if o.order_id != order_id]
            return
        self.by_cid[cid] = {
            "order_id": order_id, "status": "FILLED", "avg": price, "qty": qty,
            "executed": qty, "fee": fee, "kw": kw, "position_id": pid, "filled_at": _now(),
        }
