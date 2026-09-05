"""Support/Resistance bounce strategy.

Trades reversals at structural levels: LONG when price is near a support
level, SHORT when price is near a resistance level. The levels come from the
Structure feature metadata (clustered swing lows/highs via
``agent.core.support_resistance``) — no raw OHLCV reads happen here. The
Decision pipeline (Trend -> Liquidity -> Structure -> ...) aggregates weighted
confidence, and the RiskEngine attaches ATR-based SL/TP before a Decision is
emitted.
"""

import time
from typing import Any, Optional

from agent.strategies.base import BaseStrategy
from agent.strategies.decision import (
    DecisionEngine,
    RiskEngine,
    build_decision,
)


class SupportResistanceStrategy(BaseStrategy):
    name = "support_resistance"

    def __init__(self, config, strat_cfg, exchange, notifier, feature_engine=None):
        super().__init__(config, strat_cfg, exchange, notifier, feature_engine=feature_engine)
        self.zone_pct = float(self.strat_cfg.get("zone_pct", 0.8)) / 100.0
        self.min_distance_pct = float(self.strat_cfg.get("min_distance_pct", 0.3)) / 100.0
        self.require_reversal = bool(self.strat_cfg.get("require_reversal", True))
        self.cooldown_seconds = int(self.strat_cfg.get("cooldown_seconds", 900))
        self.oppose_limit = float(self.strat_cfg.get("oppose_limit", -0.5))
        self.directions = {
            d: bool(self.strat_cfg.get("directions", {}).get(d, True)) for d in ("LONG", "SHORT")
        }
        self._last_signal = {}

        weights = dict(self.strat_cfg.get("decision_weights", {}) or {})
        self.decision = DecisionEngine(config, weights=weights, feature_engine=feature_engine)
        self.risk_engine = RiskEngine(config)

    def _sr_zone(self, features):
        """Nearest support below / resistance above the current price."""
        meta = features.structure.metadata
        price = float(meta.get("price") or 0.0)
        support = sorted([float(s) for s in meta.get("support", []) if float(s) < price])
        resistance = sorted([float(r) for r in meta.get("resistance", []) if float(r) > price])
        return price, (support[-1] if support else None), (resistance[0] if resistance else None)

    def _side_from_levels(self, features):
        """Pick a side when price sits inside an S/R zone, else None."""
        price, sup, res = self._sr_zone(features)
        if price <= 0:
            return None, None, "price tidak tersedia"
        if sup is not None and (price - sup) / price <= self.zone_pct:
            if (price - sup) / price >= self.min_distance_pct:
                return "LONG", sup, f"LONG dekat support {sup:.4g} ({(price - sup) / price * 100:.2f}%)"
            return None, sup, f"terlalu dekat support {sup:.4g}"
        if res is not None and (res - price) / price <= self.zone_pct:
            if (res - price) / price >= self.min_distance_pct:
                return "SHORT", res, f"SHORT dekat resistance {res:.4g} ({(res - price) / price * 100:.2f}%)"
            return None, res, f"terlalu dekat resistance {res:.4g}"
        return None, None, "S/R netral (tidak dalam zona)"

    def generate_signal(self, symbol, df, features=None):
        verdict = self.decision.decide(symbol, df, features)
        if verdict is None:
            return None
        fs = verdict["features"]

        side, level, note = self._side_from_levels(fs)
        if side is None:
            return None
        if not self.directions.get(side):
            return None

        side_sign = 1 if side == "LONG" else -1
        if verdict["margin"] * side_sign < self.oppose_limit:
            return None

        if self.require_reversal:
            pat_dir = (fs.structure.metadata.get("pattern") or {}).get("direction")
            if side == "LONG" and pat_dir == "bearish":
                return None
            if side == "SHORT" and pat_dir == "bullish":
                return None

        now = time.time()
        last = self._last_signal.get(symbol)
        if last and now - last < self.cooldown_seconds:
            return None
        self._last_signal[symbol] = now

        price = verdict["price"]
        risk = self.risk_engine.compute(price, side, verdict["atr"])
        if risk is None:
            return None

        confidence = min(0.95, 0.55 + 0.5 * max(0.0, verdict["margin"] * side_sign))
        if level and price:
            proximity = abs(price - level) / price
            bonus = 0.15 * max(0.0, 1.0 - proximity / self.zone_pct) if self.zone_pct > 0 else 0.0
            confidence = min(0.95, confidence + bonus)

        reasons = list(verdict["reasons"])
        reasons.append(f"support_resistance: {note}")

        metadata = {
            "level": round(float(level), 8) if level is not None else None,
            "side": side,
            "proximity_pct": round(abs(price - level) / price * 100.0, 4) if level and price else None,
            "margin": verdict["margin"],
            "support": fs.structure.metadata.get("support"),
            "resistance": fs.structure.metadata.get("resistance"),
        }
        return build_decision(self.name, symbol, side, confidence, reasons, risk, price, metadata)
