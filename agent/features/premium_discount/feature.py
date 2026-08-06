"""PremiumDiscountFeature — BaseFeature wrapper around the analyzer.

Exposes the deterministic premium/discount analysis as a Feature Engine
feature (df-only, opt-in). SMC convention: buy in the discount zone (price
below equilibrium), sell in the premium zone (price above equilibrium). The
signal is ``bullish`` in discount, ``bearish`` in premium, and ``neutral`` on
equilibrium/unknown. ``metadata`` carries the full result dict.
"""

from typing import Any

from agent.features.base import BaseFeature, FeatureResult
from agent.features.premium_discount.analyzer import PremiumDiscountAnalyzer

_ZONE_SIGNAL = {
    "discount": "bullish",
    "premium": "bearish",
    "equilibrium": "neutral",
    "unknown": "neutral",
}


class PremiumDiscountFeature(BaseFeature):
    name = "premium_discount"
    df_only = True
    opt_in = True

    def _load_config(self) -> None:
        super()._load_config()
        self._analyzer = PremiumDiscountAnalyzer(self.config)

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        result = self._analyzer.analyze(symbol, df)
        zone = result["zone"]
        signal = _ZONE_SIGNAL[zone]
        confidence = 0.5
        if zone in ("discount", "premium"):
            eq = float(result["equilibrium"])
            price = float(result["price"])
            half_span = float(result["range"]) / 2.0
            depth = abs(price - eq) / half_span if half_span > 0 else 0.0
            confidence = round(0.5 + 0.4 * min(max(depth, 0.0), 1.0), 4)

        metadata = dict(result)
        metadata.pop("symbol", None)
        return FeatureResult(self.name, signal, confidence, metadata)
