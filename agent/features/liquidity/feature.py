"""SmartMoneyLiquidityFeature — BaseFeature wrapper around the analyzer.

Exposes the deterministic structural liquidity analysis as a Feature Engine
feature (df-only: no market data needed). The feature signal mirrors the
latest liquidity-sweep direction (``bullish``/``bearish``) or ``neutral``;
the full analysis dict is exposed in ``metadata``.
"""

from typing import Any

from agent.features.base import BaseFeature, FeatureResult
from agent.features.liquidity.smart_money import SmartMoneyLiquidityAnalyzer

_SIGNAL_FROM_DIRECTION = {"bullish": "bullish", "bearish": "bearish"}


class SmartMoneyLiquidityFeature(BaseFeature):
    name = "smart_money_liquidity"
    df_only = True
    opt_in = True

    def _load_config(self) -> None:
        super()._load_config()
        self._analyzer = SmartMoneyLiquidityAnalyzer(self.config)

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        result = self._analyzer.analyze(symbol, df)
        if result.get("liquidity_sweep"):
            signal = _SIGNAL_FROM_DIRECTION.get(result.get("sweep_direction"), "neutral")
        else:
            signal = "neutral"
        metadata = dict(result)
        metadata.pop("symbol", None)
        return FeatureResult(self.name, signal, result.get("confidence", 0.5), metadata)
