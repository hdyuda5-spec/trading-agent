"""MarketStructureFeature — BaseFeature wrapper around the analyzer.

Exposes the deterministic SMC structure analysis as a Feature Engine feature
(df-only: no market data needed). The signal follows the swing trend and
flips to the new direction on a confirmed shift: MSS direction > CHOCH
direction > BOS direction > trend. The full analysis dict is exposed in
``metadata``.
"""

from typing import Any

from agent.features.base import BaseFeature, FeatureResult
from agent.features.market_structure.analyzer import MarketStructureAnalyzer


class MarketStructureFeature(BaseFeature):
    name = "market_structure"
    df_only = True
    opt_in = True

    def _load_config(self) -> None:
        super()._load_config()
        self._analyzer = MarketStructureAnalyzer(self.config)

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        result = self._analyzer.analyze(symbol, df)
        if result.get("mss"):
            signal = result["mss_direction"]
        elif result.get("choch"):
            signal = result["choch_direction"]
        elif result.get("bos"):
            signal = result["bos_direction"]
        elif result.get("trend") in ("bullish", "bearish"):
            signal = result["trend"]
        else:
            signal = "neutral"
        metadata = dict(result)
        metadata.pop("symbol", None)
        return FeatureResult(self.name, signal, result.get("confidence", 0.5), metadata)
