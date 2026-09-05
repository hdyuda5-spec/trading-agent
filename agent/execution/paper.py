"""Paper trading exchange — zero-risk substitute for the real ExchangeClient.

``PaperExchange`` delegates all *market data* reads to the underlying real
exchange (live prices, books, OHLCV) but simulates the account: balance,
positions and orders live in memory. A trade in paper mode is therefore
priced with real market prices but never touches real funds.

Switching is a one-line config decision: ``trading.mode = "paper"|"live"``
(default ``paper``). The rest of the stack (PortfolioService, OrderManager,
SignalService) is unchanged because the exchange interface is identical.
"""

import logging
import threading
import time
from typing import List, Optional

logger = logging.getLogger("trading-agent")


class PaperExchange:
    def __init__(self, real, initial_balance_usdt: float = 1000.0, fee_pct: float = 0.05):
        self.real = real
        self.testnet = getattr(real, "testnet", False)
        self._initial = float(initial_balance_usdt)
        self._fee_pct = float(fee_pct)
        self._lock = threading.RLock()
        self._balance = self._initial
        self._positions = {}   # symbol -> dict
        self._orders = {}      # order_id -> dict
        self._order_seq = 0

    # -- account simulation ----------------------------------------------

    def _mk_cash(self):
        return {"USDT": {"free": round(self._balance - self._pending_buy_value(), 6),
                         "used": 0.0, "total": round(self._balance, 6)}}

    def _pending_buy_value(self):
        return sum(float(o.get("cost") or 0) for o in self._orders.values()
                   if o["side"] == "buy" and o["status"] == "open")

    def set_balance(self, usdt: float) -> None:
        with self._lock:
            self._balance = float(usdt)

    def reset(self) -> None:
        with self._lock:
            self._balance = self._initial
            self._positions.clear()
            self._orders.clear()

    # -- delegated market data --------------------------------------------

    @property
    def client(self):
        return self.real.client

    def check_health(self):
        try:
            return bool(self.real.check_health())
        except Exception:
            return True

    def fetch_tickers(self):
        return self.real.fetch_tickers()

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=100):
        return self.real.fetch_ohlcv(symbol, timeframe, limit)

    def fetch_ticker(self, symbol):
        return self.real.fetch_ticker(symbol)

    def fetch_order_book(self, symbol, limit=5):
        return self.real.fetch_order_book(symbol, limit)

    def _price(self, symbol):
        try:
            t = self.real.fetch_ticker(symbol)
            p = float(t.get("last") or 0)
            if p > 0:
                return p
        except Exception:
            pass
        return 0.0

    # -- positions --------------------------------------------------------

    def fetch_positions(self, symbols=None):
        with self._lock:
            out = [
                {
                    "symbol": s, "contracts": p["contracts"], "side": p["side"],
                    "entry": p["entry"], "notional": p["notional"], "unrealizedPnl": p.get("unrealizedPnl", 0.0),
                    "info": {"positionAmt": p["contracts"], "side": p["side"]},
                }
                for s, p in self._positions.items() if float(p["contracts"]) != 0
            ]
        for pos in out:
            p = self._price(pos["symbol"])
            if p > 0:
                entry = float(pos["entry"]) or p
                mark = (p - entry) if pos["side"] == "long" else (entry - p)
                pos["unrealizedPnl"] = round(mark * float(pos["contracts"]), 6)
        if symbols:
            symset = set(symbols)
            out = [p for p in out if p["symbol"] in symset]
        return out

    def fetch_balance(self):
        return self._mk_cash()

    def fetch_balance_contracts(self):
        return {"USDT": self._mk_cash()["USDT"]}

    def fetch_my_trades(self, symbol, since=None, limit=50, params=None):
        return []

    def fetch_trades(self, symbol, since=None, limit=50, params=None):
        return []

    def set_leverage(self, leverage, symbol=None, params=None):
        return {"leverage": float(leverage)}

    def set_margin_mode(self, mode, symbol=None, params=None):
        return {"marginMode": mode}

    # -- orders -----------------------------------------------------------

    def _next_id(self, prefix="pap"):
        self._order_seq += 1
        return f"{prefix}{int(time.time())}{self._order_seq}"

    def create_order(self, symbol, otype, side, amount, price=None, params=None):
        params = params or {}
        oid = self._next_id()
        with self._lock:
            px = float(price) if price else self._price(symbol)
            if px <= 0:
                raise RuntimeError(f"paper: no live price for {symbol}")
            cost = float(amount) * px
            fee = cost * (self._fee_pct / 100.0)
            order = {
                "id": oid, "symbol": symbol, "type": otype, "side": side,
                "amount": float(amount), "price": px, "cost": cost,
                "status": "closed", "average": px, "fee": {"cost": fee, "currency": "USDT"},
                "reduceOnly": bool(params.get("reduceOnly")), "info": {"orderId": oid},
                "timestamp": int(time.time() * 1000),
            }
            self._orders[oid] = order
            self._apply_fill(symbol, side, float(amount), px, fee, order)
        return order

    def create_stop_order(self, symbol, side, stop_price, amount=None, params=None):
        params = params or {}
        oid = self._next_id("papStop")
        with self._lock:
            order = {
                "id": oid, "symbol": symbol, "type": "stop", "side": side,
                "amount": float(amount or 0), "stopPrice": float(stop_price),
                "status": "open", "reduceOnly": True, "info": {"orderId": oid},
                "timestamp": int(time.time() * 1000),
            }
            self._orders[oid] = order
        return order

    def _apply_fill(self, symbol, side, amount, px, fee, order):
        pos = self._positions.get(symbol)
        if pos is None or float(pos["contracts"]) == 0:
            if side == "buy":
                self._positions[symbol] = {"contracts": amount, "side": "long",
                                           "entry": px, "notional": amount * px}
            else:
                self._positions[symbol] = {"contracts": amount, "side": "short",
                                           "entry": px, "notional": amount * px}
            order["status"] = "closed"
            return
        if side == "buy" and pos["side"] == "long":
            total = pos["contracts"] + amount
            pos["entry"] = (pos["contracts"] * pos["entry"] + amount * px) / total
            pos["contracts"] = total
        elif side == "sell" and pos["side"] == "short":
            total = pos["contracts"] + amount
            pos["entry"] = (pos["contracts"] * pos["entry"] + amount * px) / total
            pos["contracts"] = total
        else:
            reduce = min(pos["contracts"], amount)
            entry = pos["entry"]
            pnl = (px - entry) * reduce if pos["side"] == "long" else (entry - px) * reduce
            self._balance += pnl - fee
            pos["contracts"] -= reduce
            if float(pos["contracts"]) <= 0:
                self._positions.pop(symbol)
        order["status"] = "closed"

    def fetch_order(self, order_id, symbol=None):
        order = self._orders.get(order_id)
        if order is None:
            raise Exception(f"Order not found: {order_id}")
        return order

    def fetch_open_orders(self, symbol=None):
        with self._lock:
            rows = [o for o in self._orders.values() if o["status"] == "open"]
        if symbol:
            rows = [o for o in rows if o["symbol"] == symbol]
        return rows

    def fetch_open_stop_orders(self, symbol=None):
        rows = [o for o in self.fetch_open_orders(symbol) if o.get("type") == "stop"]
        return rows

    def cancel_order(self, order_id, symbol=None):
        order = self._orders.get(order_id)
        if order is None:
            raise Exception(f"Order not found: {order_id}")
        order["status"] = "canceled"
        return order

    def cancel_stop_order(self, algo_id, symbol=None):
        if algo_id in self._orders:
            self.cancel_order(algo_id, symbol)
        return {"id": algo_id, "status": "canceled"}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "balance_usdt": round(self._balance, 6),
                "positions": {s: dict(p) for s, p in self._positions.items()},
                "open_orders": len([o for o in self._orders.values() if o["status"] == "open"]),
            }