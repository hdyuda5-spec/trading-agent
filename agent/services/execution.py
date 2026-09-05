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
    def __init__(self, config, exchange, risk, orders, notifier, strategies, whale, portfolio,
                 *, store=None, funnel=None):
        self.config = config
        self.exchange = exchange
        self.risk = risk
        self.orders = orders
        self.notifier = notifier
        self.strategies = strategies
        self.whale = whale
        self.portfolio = portfolio
        self.store = store
        self.funnel = funnel
        self.filters = TradeFilters(config)

    # -- order lifecycle -------------------------------------------------

    def open_position(self, symbol, signal, equity, atr, ticket=None):
        order = self.orders.open_position(symbol, signal, equity, atr, ticket=ticket)
        if order is not None and ticket is not None:
            if self.funnel:
                self.funnel.inc("fills")
                self.funnel.notify("ticket_filled", symbol=symbol, ticket_id=ticket.ticket_id)
                try:
                    self.funnel.save()
                except Exception:
                    pass
            self._persist_ticket(ticket)
        elif order is None and ticket is not None:
            status = "EXPIRED" if ticket.expired() else "CANCELLED"
            ticket.mark(status, "ticket tidak terisi")
            if self.funnel:
                self.funnel.inc("expired" if status == "EXPIRED" else "cancelled")
                try:
                    self.funnel.save()
                except Exception:
                    pass
            self._persist_ticket(ticket)
        return order

    def _persist_ticket(self, ticket):
        if self.store is None:
            return
        try:
            self.store.save_ticket(ticket)
        except Exception:
            pass

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
        desired = min(self.risk.notional(equity, price, atr), budget)
        if desired <= 0:
            return False, None, "budget habis"
        equity_eff = self.risk.equity_for_notional(desired, price, atr)
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
