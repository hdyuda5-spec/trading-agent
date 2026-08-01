from agent.strategies.base import BaseStrategy


class GridStrategy(BaseStrategy):
    name = "grid"

    def __init__(self, config, strat_cfg, exchange, notifier):
        super().__init__(config, strat_cfg, exchange, notifier)
        self.grids = {}
        self.center = {}

    def generate_signal(self, symbol, df):
        return None

    def build_grid(self, symbol, price):
        levels = self.strat_cfg["grid_levels"]
        spacing = self.strat_cfg["grid_spacing_pct"] / 100.0
        grid = []
        for i in range(1, levels + 1):
            grid.append({
                "symbol": symbol,
                "pair": i,
                "side": "buy",
                "price": round(price * (1 - spacing * i), 8),
                "status": "pending",
            })
            grid.append({
                "symbol": symbol,
                "pair": i,
                "side": "sell",
                "price": round(price * (1 + spacing * i), 8),
                "status": "pending",
            })
        self.grids[symbol] = grid
        self.center[symbol] = price
        return grid

    def reset(self, symbol):
        self.grids.pop(symbol, None)
        self.center.pop(symbol, None)

    def needs_rebuild(self, symbol, price):
        center = self.center.get(symbol)
        if center is None or center <= 0:
            return True
        drift = self.strat_cfg.get("grid_rebuild_drift_pct", 3.0) / 100.0
        return abs(price - center) / center > drift

    def reconcile(self, symbol, positions, open_orders, price=None):
        grid = self.grids.get(symbol, [])
        if not grid:
            return []
        open_prices = {float(o.get("price")) for o in open_orders if o.get("symbol") == symbol}
        if price is None or price <= 0:
            price = self._current_price(symbol)
        actions = []
        for level in grid:
            if level["status"] == "placed":
                if level["price"] not in open_prices:
                    level["status"] = "filled"
                continue
            if level["status"] == "filled":
                continue
            if level["side"] == "buy":
                if level["price"] <= price:
                    actions.append(level)
            else:
                buy_pair = self._pair(symbol, level["pair"])
                if buy_pair and buy_pair["status"] == "filled":
                    actions.append(level)
        return actions

    def _pair(self, symbol, pair_id):
        for level in self.grids.get(symbol, []):
            if level["pair"] == pair_id and level["side"] == "buy":
                return level
        return None

    def order_size(self, symbol, equity, price):
        per_level_pct = self.strat_cfg.get("grid_per_level_pct") or (5.0 / max(self.strat_cfg["grid_levels"], 1))
        notional = equity * (per_level_pct / 100.0)
        return round(notional / price, 8)

    def _current_price(self, symbol):
        try:
            return float(self.exchange.fetch_ticker(symbol)["last"])
        except Exception:
            return 0.0
