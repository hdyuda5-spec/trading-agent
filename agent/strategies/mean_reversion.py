"""Mean-reversion strategy — consumes Feature Engine outputs only.

No indicator is computed here: Bollinger bands come from the Volatility
feature, RSI from the Trend feature, and the S/R / order-block / FVG context
comes from the decision pipeline. The strategy triggers when price trades
through a band against RSI (the classic fade), then the weighted feature votes
modulate confidence before the Risk Engine attaches SL/TP.
"""

import time
from typing import Any, Optional

from agent.strategies.base import BaseStrategy
from agent.strategies.decision import (
    DecisionEngine,
    RiskEngine,
    build_decision,
)

_MR_WEIGHTS = {
    "trend": 0.10,
    "smart_money_liquidity": 0.15,
    "structure": 0.30,
    "order_blocks": 0.20,
    "fvg": 0.15,
}


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"

    def __init__(self, config, strat_cfg, exchange, notifier, feature_engine=None):
        super().__init__(config, strat_cfg, exchange, notifier, feature_engine=feature_engine)
        self.rsi_oversold = float(self.strat_cfg.get("rsi_oversold", 30))
        self.rsi_overbought = float(self.strat_cfg.get("rsi_overbought", 70))
        self.cooldown_seconds = int(self.strat_cfg.get("cooldown_seconds", 900))
        self.oppose_limit = float(self.strat_cfg.get("oppose_limit", -0.35))
        self._last_signal = {}

        weights = dict(_MR_WEIGHTS)
        weights.update(self.strat_cfg.get("decision_weights", {}) or {})
        self.decision = DecisionEngine(config, weights=weights, feature_engine=feature_engine)
        self.risk_engine = RiskEngine(config)

    def _gate(self, features, price):
        """Band + RSI fade from feature metadata; returns (side, strength, rsi)."""
        vola = features.volatility.metadata
        lower = vola.get("bb_lower")
        upper = vola.get("bb_upper")
        rsi = features.trend.metadata.get("rsi")
        if lower is None or upper is None or rsi is None:
            return None, 0.0, rsi
        strength = 0.0
        if price <= lower and rsi < self.rsi_oversold:
            side = "LONG"
            if rsi < self.rsi_oversold - 10:
                strength += 0.15
        elif price >= upper and rsi > self.rsi_overbought:
            side = "SHORT"
            if rsi > self.rsi_overbought + 10:
                strength += 0.15
        else:
            side = None
        return side, strength, rsi

    def generate_signal(self, symbol, df, features=None):
        verdict = self.decision.decide(symbol, df, features)
        if verdict is None:
            return None
        fs = verdict["features"]
        price = verdict["price"]

        side, strength, rsi = self._gate(fs, price)
        if side is None:
            return None

        side_sign = 1 if side == "LONG" else -1
        alignment = max(0.0, verdict["margin"] * side_sign)
        if verdict["margin"] * side_sign < self.oppose_limit:
            return None

        now = time.time()
        last = self._last_signal.get(symbol)
        if last and now - last < self.cooldown_seconds:
            return None
        self._last_signal[symbol] = now

        risk = self.risk_engine.compute(price, side, verdict["atr"])
        if risk is None:
            return None

        confidence = min(0.92, 0.55 + strength + 0.25 * alignment)
        reasons = list(verdict["reasons"])
        rsi_txt = f", rsi {rsi:.1f}" if rsi is not None else ""
        reasons.append(f"mean_reversion: fade {side} (band + rsi{rsi_txt})")

        vola = fs.volatility.metadata
        metadata = {
            "bb_period": vola.get("bb_period"),
            "bb_std": vola.get("bb_std"),
            "sma": vola.get("bb_sma"),
            "upper": vola.get("bb_upper"),
            "lower": vola.get("bb_lower"),
            "rsi": round(rsi, 2) if rsi is not None else None,
            "margin": verdict["margin"],
            "alignment": round(alignment, 4),
        }

        return build_decision(self.name, symbol, side, confidence, reasons, risk, price, metadata)
