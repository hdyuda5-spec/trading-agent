class RiskManager:
    def __init__(self, config, exchange):
        self.cfg = config
        self.exchange = exchange
        self.initial_equity = None
        self.peak_equity = None

    def set_initial_equity(self, equity):
        self.initial_equity = equity
        self.peak_equity = equity

    def update_equity(self, equity):
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

    def compute_position_size(self, symbol, price, equity, direction, atr=None):
        max_pos = self.cfg["max_position_pct"]
        qty_usd = equity * (max_pos / 100.0)
        if atr and price and self.cfg.get("adaptive_sizing", False):
            bounds = self.cfg.get("size_volatility_bounds", [0.5, 2.0])
            normal_pct = self.cfg.get("atr_normal_pct", 1.0) / 100.0
            atr_pct = atr / price
            if atr_pct > 0:
                factor = normal_pct / atr_pct
                factor = max(bounds[0], min(bounds[1], factor))
                qty_usd *= factor
        size = qty_usd / price
        return size

    def exposure_ok(self, positions, equity=None):
        total_notional = 0.0
        if equity is None:
            balance = self.exchange.fetch_balance()
            equity = float(balance.get("USDT", {}).get("total", 0) or 0)
        for pos in positions:
            if abs(float(pos.get("contracts") or 0)) > 0:
                total_notional += abs(float(pos.get("notional") or 0))
        max_exposure = equity * (self.cfg["max_total_exposure_pct"] / 100.0)
        return total_notional + self._pending_notional() <= max_exposure, total_notional

    def _pending_notional(self):
        try:
            total = 0.0
            for o in self.exchange.fetch_open_orders():
                amount = float(o.get("amount") or 0)
                price = float(o.get("price") or 0)
                total += amount * price
            return total
        except Exception:
            return 0.0

    def check_fee_tolerance(self, symbol, fee_pct=0.02):
        limit = abs(self.cfg.get("min_fee_tolerance_pct", 0.15))
        if limit <= 0:
            return True, 0.0
        try:
            book = self.exchange.fetch_order_book(symbol, 5)
            if not book or not book.get("bids") or not book.get("asks"):
                return False, float("inf")
            bid = float(book["bids"][0][0])
            ask = float(book["asks"][0][0])
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid * 100.0
            return (spread_pct + fee_pct) <= limit, round(spread_pct, 4)
        except Exception:
            return False, float("inf")

    def can_open(self, symbol, positions, equity, price, side):
        if self.daily_loss_exceeded(equity):
            return False, "daily loss limit reached"
        if len([p for p in positions if abs(float(p.get("contracts") or 0)) > 0]) >= self.cfg["max_open_positions"]:
            return False, "max open positions reached"
        ok, _ = self.exposure_ok(positions, equity)
        if not ok:
            return False, "max total exposure reached"
        return True, "ok"

    def daily_loss_exceeded(self, equity):
        if self.initial_equity is None or self.initial_equity <= 0:
            return False
        if equity <= 0:
            return True
        loss_pct = ((self.initial_equity - equity) / self.initial_equity) * 100.0
        return loss_pct >= abs(self.cfg["daily_loss_limit_pct"])

    def below_min_equity(self, equity):
        min_eq = float(self.cfg.get("min_equity_usdt", 20))
        return min_eq > 0 and equity < min_eq

    def trailing_stop_hit(self, side, best_price, current):
        pct = self.cfg.get("trailing_stop_pct", 0.0) / 100.0
        if pct <= 0:
            return False
        if side == "long":
            return current <= best_price * (1 - pct)
        return current >= best_price * (1 + pct)

    def build_take_profit(self, entry_price, side, atr=None):
        if atr and self.cfg.get("use_atr_stops", False):
            mult = self.cfg.get("atr_tp_mult", 2.5)
            if side == "buy":
                return round(entry_price + mult * atr, 8)
            return round(entry_price - mult * atr, 8)
        pct = self.cfg["take_profit_pct"] / 100.0
        if side == "buy":
            return round(entry_price * (1 + pct), 8)
        return round(entry_price * (1 - pct), 8)

    def build_stop_loss(self, entry_price, side, atr=None):
        if atr and self.cfg.get("use_atr_stops", False):
            mult = self.cfg.get("atr_stop_mult", 1.5)
            if side == "buy":
                return round(entry_price - mult * atr, 8)
            return round(entry_price + mult * atr, 8)
        pct = self.cfg["stop_loss_pct"] / 100.0
        if side == "buy":
            return round(entry_price * (1 - pct), 8)
        return round(entry_price * (1 + pct), 8)

    def enforce_leverage(self, symbol):
        leverage = min(self.cfg["leverage"], self.cfg["max_leverage"])
        self.exchange.set_margin_mode(symbol, "isolated")
        self.exchange.set_leverage(symbol, leverage)
        return leverage
