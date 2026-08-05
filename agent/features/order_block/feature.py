"""OrderBlockFeature — BaseFeature wrapper around the analyzer.

Exposes the deterministic order block analysis as a Feature Engine feature
(df-only: no market data needed). The signal follows the highest-ranked
still-actionable block: a bullish/bearish unmitigated block, or the flipped
polarity of a breaker block; else ``neutral``. The ranked block list is
exposed in ``metadata``.
"""

from typing import Any

from agent.features.base import BaseFeature, FeatureResult
from agent.features.order_block.analyzer import OrderBlockAnalyzer

_BREAKER_SIGNAL = {"bullish": "bearish", "bearish": "bullish"}


class OrderBlockFeature(BaseFeature):
    name = "order_blocks"
    df_only = True
    opt_in = True

    def _load_config(self) -> None:
        super()._load_config()
        self._analyzer = OrderBlockAnalyzer(self.config)

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        result = self._analyzer.analyze(symbol, df)
        signal = "neutral"
        confidence = 0.5
        best = None
        for block in result["order_blocks"]:
            if block["status"] == "unmitigated" or block["breaker"]:
                best = block
                break
        if best is not None:
            if best["direction"] == "breaker":
                signal = _BREAKER_SIGNAL[best["original_direction"]]
            else:
                signal = best["direction"]
            confidence = round(0.5 + 0.4 * best["quality"], 4)

        metadata = dict(result)
        metadata.pop("symbol", None)
        metadata["best_actionable"] = best
        return FeatureResult(self.name, signal, confidence, metadata)
