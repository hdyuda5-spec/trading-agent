"""Execution service — order placement and trade-level gating.

Owns everything that touches the order lifecycle: opening a position through
``OrderManager``, the screener budget/sizing preflight (``screen_trade_ok``),
stale-order cleanup, SL/TP re-guarding and the grid strategy loop. Strategies
(which emit Decisions) never place orders directly — they always go through
this service.
"""

import logging
from typing import List, Optional

from agent.core.utils import is_valid_atr
from agent.services.filters import TradeFilters

logger = logging.getLogger("trading-agent")


class ExecutionService:
    def __init__(self, config, exchange, risk, orders, notifier, strategies, whale, portfolio):
        self.config = config
        self.exchange = exchange
        self.risk = risk
        self.orders = orders
        self.notifier = notifier
        self.strategies = strategies
        self.whale = whale
        self.portfolio = portfolio
        self.filters = TradeFilters(config)

    # -- order lifecycle -------------------------------------------------

    def open_position(self, symbol, signal, equity, atr):
        return self.orders.open_position(symbol, signal, equity, atr)

    def cancel_stale_orders(self, ttl: int = 0) -> None:
        try:
            self.orders.cancel_stale_orders(ttl=ttl, symbol=None)
        except Exception as e:
            logger.warning("cancel_stale_orders failed: %s", e)

    def ensure_sl_tp(self, symbol, side, entry, contracts, atr) -> None:
        self.orders.ensure_sl_tp(symbol, side, entry, contracts, atr)

    # -- screener preflight ----------------------------------------------

    def screen_trade_ok(self, symbol, side, price, atr, equity, positions):
        m = self.exchange.client.markets.get(symbol)
        if m is None:
            return False, None, "market tidak ditemukan"
        limits = m.get("limits", {})
        min_cost = float((limits.get("cost") or {}).get("min") or 0)
        min_amt = float((limits.get("amount") or {}).get("min") or 0)
        max_pos_pct = self.config["risk"].get("max_position_pct", 60) / 100.0
        exposure_pct = self.config["risk"].get("max_total_exposure_pct", 90) / 100.0
        ok, spread = self.risk.check_fee_tolerance(symbol)
        if not ok:
            return False, None, f"spread/fee {spread}%"
        try:
            pending = self.risk._pending_notional()
        except Exception:
            pending = 0.0
        used = 0.0
        for p in positions:
            if abs(float(p.get("contracts") or 0)) > 0:
                used += abs(float(p.get("notional") or 0))
        budget = max(0.0, equity * exposure_pct - used - pending)
        if min_cost and budget < min_cost:
            return False, None, f"budget {budget:.2f} < minCost {min_cost:.0f}"
        factor = 1.0
        if atr and price:
            atr_pct = atr / price
            if atr_pct > 0:
                normal_pct = self.config["risk"].get("atr_normal_pct", 1.0) / 100.0
                bounds = self.config["risk"].get("size_volatility_bounds", [0.9, 2.0])
                factor = max(bounds[0], min(bounds[1], normal_pct / atr_pct))
        desired = min(equity * max_pos_pct * factor, budget)
        if desired <= 0:
            return False, None, "budget habis"
        equity_eff = desired / (max_pos_pct * factor) if max_pos_pct * factor > 0 else equity
        qty = self.risk.compute_position_size(symbol, price, equity_eff, "buy" if side == "LONG" else "sell", atr)
        try:
            qty = float(self.exchange.client.amount_to_precision(symbol, qty))
        except Exception:
            return False, None, "qty di bawah presisi pasar"
        cost = qty * price
        if min_cost and cost < min_cost:
            return False, None, f"minCost {min_cost:.0f} > {cost:.2f}"
        if min_amt and qty < min_amt:
            return False, None, f"minAmt {min_amt} > {qty}"
        allowed, reason = self.risk.can_open(symbol, positions, equity, price, side)
        if not allowed:
            return False, None, reason
        return True, equity_eff, "ok"

    # -- grid ------------------------------------------------------------

    def run_grid(self, symbol, positions, equity, atr) -> None:
        for strategy in self.strategies:
            if strategy.name != "grid":
                continue
            try:
                self._run_grid_strategy(strategy, symbol, positions, equity, atr)
            except Exception as e:
                self.notifier.alert(f"Grid gagal {symbol}", str(e))

    def _run_grid_strategy(self, strategy, symbol, positions, equity, atr) -> None:
        ticker = self.exchange.fetch_ticker(symbol)
        price = float(ticker["last"])
        open_orders = self.exchange.fetch_open_orders(symbol)
        if not strategy.grids.get(symbol):
            strategy.build_grid(symbol, price)
        elif strategy.needs_rebuild(symbol, price):
            self.orders.cancel_pending(symbol)
            strategy.reset(symbol)
            strategy.build_grid(symbol, price)
            open_orders = self.exchange.fetch_open_orders(symbol)
        amount = strategy.order_size(symbol, equity, price)
        max_orders = strategy.strat_cfg.get("max_grid_orders", 0)
        pending = len(open_orders)
        for level in strategy.reconcile(symbol, positions, open_orders, price):
            if max_orders and pending >= max_orders:
                break
            try:
                self.exchange.create_order(
                    symbol,
                    "limit",
                    level["side"],
                    amount=amount,
                    price=level["price"],
                    params={"postOnly": True},
                )
                self.notifier.info(f"[GRID] {symbol} {level['side']} @ {level['price']}")
                setup = self.portfolio.capture_setup(
                    symbol, "LONG" if level["side"] == "buy" else "SHORT", level["price"], atr
                )
                self.portfolio.set_trade_meta(symbol, "grid", setup, 50.0)
                level["status"] = "placed"
                pending += 1
            except Exception as e:
                self.notifier.alert(f"Grid order failed {symbol}", str(e))
