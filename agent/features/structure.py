"""Structure feature.

Captures price structure: candlestick patterns (reusing ``agent.core.patterns``)
and support/resistance levels (reusing ``agent.core.support_resistance``).
Levels are exposed in metadata so strategies can apply their own configurable
proximity rules without re-reading OHLCV.
"""

from typing import Any

from agent.core.patterns import detect_patterns
from agent.core.support_resistance import SupportResistance
from agent.features.base import BaseFeature, FeatureResult


class StructureFeature(BaseFeature):
    name = "structure"
    df_only = True

    def _load_config(self) -> None:
        super()._load_config()
        cfg = self.config.get("features", {}).get(self.name, {}) or {}
        self.sr_window = int(cfg.get("sr_window", 3))
        self.sr_cluster_pct = float(cfg.get("sr_cluster_pct", 0.5))
        self.zone_pct = float(cfg.get("zone_pct", 0.6))
        self.sr = SupportResistance(window=self.sr_window, cluster_pct=self.sr_cluster_pct)

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        n = len(df)
        if n < 5:
            return FeatureResult.neutral(self.name, metadata={"reason": "insufficient_data"})

        pattern = detect_patterns(df)
        levels = self.sr.levels(df)
        price = float(df["close"].iloc[-1])
        support = [float(x) for x in levels.get("support", [])]
        resistance = [float(x) for x in levels.get("resistance", [])]

        near_support = any(
            price > s and (price - s) / price * 100.0 <= self.zone_pct for s in support
        )
        near_resistance = any(
            r > price and (r - price) / price * 100.0 <= self.zone_pct for r in resistance
        )

        direction = (pattern or {}).get("direction")
        if direction in ("bullish", "bearish"):
            signal = direction
            confidence = 0.7
        elif near_support:
            signal, confidence = "bullish", 0.6
        elif near_resistance:
            signal, confidence = "bearish", 0.6
        else:
            signal, confidence = "neutral", 0.5

        metadata = {
            "pattern": pattern,
            "support": support,
            "resistance": resistance,
            "near_support": near_support,
            "near_resistance": near_resistance,
            "sr_zone_pct": self.zone_pct,
            "sr_window": self.sr_window,
            "sr_cluster_pct": self.sr_cluster_pct,
            "price": round(price, 8),
        }
        return FeatureResult(self.name, signal, confidence, metadata)
