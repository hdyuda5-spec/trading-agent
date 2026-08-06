"""Policy-based risk engine.

``RiskEngine`` composes small, single-responsibility policies instead of one
procedural class. Each policy owns exactly one concern — position sizing,
exposure, stop loss, take profit, leverage, drawdown or pre-trade validation —
and exposes only its own methods. The engine wires the policies together and
forwards calls, so neither the bot nor any strategy sees the internals.

Composition & injection:

- ``RiskEngine(config, exchange, policies={...})`` builds the default set and
  lets callers override any policy by name (``policies={"position_sizing": ...}``)
  or inject entirely new ones later without touching existing code.
- Policies that depend on peers receive them via constructor injection
  (``ValidationPolicy`` receives the exposure and drawdown policies).

Backward compatibility:

- ``RiskManager`` remains as an alias of ``RiskEngine``; existing
  ``from agent.core.risk import RiskManager`` imports keep working.
- Every public method, signature and return shape of the old ``RiskManager``
  is preserved, including the ``_pending_notional()`` private alias.
- ``initial_equity`` / ``peak_equity`` attribute access is preserved via
  read-only properties backed by the ``DrawdownPolicy``.
"""

from typing import Any, Dict, List, Optional

from agent.core.utils import is_valid_atr


class BasePolicy:
    """Marker base for risk policies. ``name`` must be unique per engine."""

    name = "base"


class PositionSizingPolicy(BasePolicy):
    """Converts equity + price (+ optional ATR) into a base quantity."""

    name = "position_sizing"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def compute_position_size(self, symbol, price, equity, direction, atr=None):
        max_pos = self.cfg["max_position_pct"]
        qty_usd = equity * (max_pos / 100.0)
        if is_valid_atr(atr) and price and self.cfg.get("adaptive_sizing", False):
            bounds = self.cfg.get("size_volatility_bounds", [0.5, 2.0])
            normal_pct = self.cfg.get("atr_normal_pct", 1.0) / 100.0
            atr_pct = atr / price
            if atr_pct > 0:
                factor = normal_pct / atr_pct
                factor = max(bounds[0], min(bounds[1], factor))
                qty_usd *= factor
        size = qty_usd / price
        return size


class ExposurePolicy(BasePolicy):
    """Tracks aggregate notional across live positions and open orders."""

    name = "exposure"

    def __init__(self, cfg: dict, exchange):
        self.cfg = cfg
        self.exchange = exchange

    def pending_notional(self):
        try:
            total = 0.0
            for o in self.exchange.fetch_open_orders():
                amount = float(o.get("amount") or 0)
                price = float(o.get("price") or 0)
                total += amount * price
            return total
        except Exception:
            return 0.0

    def _pending_notional(self):
        """Backward-compat alias for the old private method name."""
        return self.pending_notional()

    def exposure_ok(self, positions, equity=None):
        total_notional = 0.0
        if equity is None:
            balance = self.exchange.fetch_balance()
            equity = float(balance.get("USDT", {}).get("total", 0) or 0)
        for pos in positions:
            if abs(float(pos.get("contracts") or 0)) > 0:
                total_notional += abs(float(pos.get("notional") or 0))
        max_exposure = equity * (self.cfg["max_total_exposure_pct"] / 100.0)
        return total_notional + self.pending_notional() <= max_exposure, total_notional


class StopLossPolicy(BasePolicy):
    """ATR- or percent-based stops and trailing-stop exit rules."""

    name = "stop_loss"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def build_stop_loss(self, entry_price, side, atr=None):
        if not is_valid_atr(atr):
            return None
        dist = self.cfg.get("atr_stop_mult", 1.5) * atr
        if dist <= 0:
            return None
        if side == "buy":
            return round(entry_price - dist, 8)
        return round(entry_price + dist, 8)

    def trailing_stop_hit(self, side, best_price, current, atr=None):
        if is_valid_atr(atr) and self.cfg.get("use_atr_trailing", False):
            dist = self.cfg.get("atr_trailing_mult", 2.0) * atr
            if side == "long":
                return current <= best_price - dist
            return current >= best_price + dist
        pct = self.cfg.get("trailing_stop_pct", 0.0) / 100.0
        if pct <= 0:
            return False
        if side == "long":
            return current <= best_price * (1 - pct)
        return current >= best_price * (1 + pct)


class TakeProfitPolicy(BasePolicy):
    """ATR-based take-profit level."""

    name = "take_profit"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def build_take_profit(self, entry_price, side, atr=None):
        if not is_valid_atr(atr):
            return None
        dist = self.cfg.get("atr_tp_mult", 2.5) * atr
        if dist <= 0:
            return None
        if side == "buy":
            return round(entry_price + dist, 8)
        return round(entry_price - dist, 8)


class LeveragePolicy(BasePolicy):
    """Per-symbol isolated margin + leverage enforcement."""

    name = "leverage"

    def __init__(self, cfg: dict, exchange):
        self.cfg = cfg
        self.exchange = exchange

    def enforce_leverage(self, symbol):
        leverage = min(self.cfg["leverage"], self.cfg["max_leverage"])
        self.exchange.set_margin_mode(symbol, "isolated")
        self.exchange.set_leverage(symbol, leverage)
        return leverage


class DrawdownPolicy(BasePolicy):
    """Equity tracking, daily-loss guard and minimum-equity guard."""

    name = "drawdown"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.initial_equity = None
        self.peak_equity = None

    def set_initial_equity(self, equity):
        self.initial_equity = equity
        self.peak_equity = equity

    def update_equity(self, equity):
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

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


class ValidationPolicy(BasePolicy):
    """Pre-trade gate combining drawdown, exposure and position-count limits."""

    name = "validation"

    def __init__(self, cfg: dict, exchange, exposure: ExposurePolicy, drawdown: DrawdownPolicy):
        self.cfg = cfg
        self.exchange = exchange
        self.exposure = exposure
        self.drawdown = drawdown

    def can_open(self, symbol, positions, equity, price, side):
        if self.drawdown.daily_loss_exceeded(equity):
            return False, "daily loss limit reached"
        if len([p for p in positions if abs(float(p.get("contracts") or 0)) > 0]) >= self.cfg["max_open_positions"]:
            return False, "max open positions reached"
        ok, _ = self.exposure.exposure_ok(positions, equity)
        if not ok:
            return False, "max total exposure reached"
        return True, "ok"

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


POLICY_FACTORIES = {
    "position_sizing": lambda cfg, exchange: PositionSizingPolicy(cfg),
    "exposure": lambda cfg, exchange: ExposurePolicy(cfg, exchange),
    "stop_loss": lambda cfg, exchange: StopLossPolicy(cfg),
    "take_profit": lambda cfg, exchange: TakeProfitPolicy(cfg),
    "leverage": lambda cfg, exchange: LeveragePolicy(cfg, exchange),
    "drawdown": lambda cfg, exchange: DrawdownPolicy(cfg),
}


class RiskEngine:
    """Composes risk policies into the single API used by the bot.

    Constructed with ``RiskEngine(config, exchange, policies={...})`` where
    ``policies`` may override any default policy by name or add new ones. All
    delegation is by composition; the engine holds no risk logic of its own.
    """

    _POLICY_NAMES = tuple(POLICY_FACTORIES)

    def __init__(self, config: dict, exchange, policies: Optional[Dict[str, Any]] = None):
        self.cfg = config
        self.exchange = exchange
        self.policies: Dict[str, BasePolicy] = self._compose(policies or {})
        self.position_sizing = self.policies["position_sizing"]
        self.exposure = self.policies["exposure"]
        self.stop_loss = self.policies["stop_loss"]
        self.take_profit = self.policies["take_profit"]
        self.leverage = self.policies["leverage"]
        self.drawdown = self.policies["drawdown"]
        self.validation = self.policies["validation"]

    def _compose(self, overrides: dict) -> Dict[str, BasePolicy]:
        built: Dict[str, BasePolicy] = {}
        for name in self._POLICY_NAMES:
            if name in overrides:
                built[name] = overrides[name]
            else:
                built[name] = POLICY_FACTORIES[name](self.cfg, self.exchange)
        built["validation"] = overrides.get(
            "validation",
            ValidationPolicy(self.cfg, self.exchange, built["exposure"], built["drawdown"]),
        )
        return built

    def policy(self, name: str) -> Optional[BasePolicy]:
        return self.policies.get(name)

    @property
    def initial_equity(self):
        return self.drawdown.initial_equity

    @property
    def peak_equity(self):
        return self.drawdown.peak_equity

    # -- Position sizing -------------------------------------------------
    def compute_position_size(self, symbol, price, equity, direction, atr=None):
        return self.position_sizing.compute_position_size(symbol, price, equity, direction, atr)

    # -- Exposure --------------------------------------------------------
    def exposure_ok(self, positions, equity=None):
        return self.exposure.exposure_ok(positions, equity)

    def pending_notional(self):
        return self.exposure.pending_notional()

    def _pending_notional(self):
        return self.exposure.pending_notional()

    # -- Stop loss / trailing -------------------------------------------
    def build_stop_loss(self, entry_price, side, atr=None):
        return self.stop_loss.build_stop_loss(entry_price, side, atr)

    def trailing_stop_hit(self, side, best_price, current, atr=None):
        return self.stop_loss.trailing_stop_hit(side, best_price, current, atr)

    # -- Take profit -----------------------------------------------------
    def build_take_profit(self, entry_price, side, atr=None):
        return self.take_profit.build_take_profit(entry_price, side, atr)

    # -- Leverage --------------------------------------------------------
    def enforce_leverage(self, symbol):
        return self.leverage.enforce_leverage(symbol)

    # -- Drawdown / equity -----------------------------------------------
    def set_initial_equity(self, equity):
        return self.drawdown.set_initial_equity(equity)

    def update_equity(self, equity):
        return self.drawdown.update_equity(equity)

    def daily_loss_exceeded(self, equity):
        return self.drawdown.daily_loss_exceeded(equity)

    def below_min_equity(self, equity):
        return self.drawdown.below_min_equity(equity)

    # -- Validation ------------------------------------------------------
    def can_open(self, symbol, positions, equity, price, side):
        return self.validation.can_open(symbol, positions, equity, price, side)

    def check_fee_tolerance(self, symbol, fee_pct=0.02):
        return self.validation.check_fee_tolerance(symbol, fee_pct)


# Backward-compatible name: existing imports of RiskManager keep working.
RiskManager = RiskEngine
