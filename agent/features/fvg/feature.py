"""FairValueGapFeature — BaseFeature wrapper around the FVG detector.

Exposes the deterministic fair value gap analysis as a Feature Engine feature
(df-only: no market data needed). The signal follows the most recent
*unmitigated* FVG formed within ``impulse_lookback`` bars of the latest bar
(``bullish``/``bearish``), else ``neutral``. The full FVG list is exposed in
``metadata``.
"""

from typing import Any

from agent.features.base import BaseFeature, FeatureResult
from agent.features.fvg.analyzer import FairValueGapAnalyzer


class FairValueGapFeature(BaseFeature):
    name = "fvg"
    df_only = True
    opt_in = True

    def _load_config(self) -> None:
        super()._load_config()
        self._analyzer = FairValueGapAnalyzer(self.config)

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        result = self._analyzer.analyze(symbol, df)
        lookback = self._analyzer.cfg.impulse_lookback
        last_bar = len(df) - 1

        latest = None
        for g in result["fvg"]:
            if g["status"] == "unmitigated" and g["index"] >= last_bar - lookback:
                if latest is None or g["index"] > latest["index"]:
                    latest = g

        signal = "neutral"
        confidence = 0.5
        if latest is not None:
            signal = latest["direction"]
            price = max(float(df["close"].iloc[-1]), 1e-9)
            confidence = round(min(0.9, 0.6 + min(0.25, latest["gap"] / price * 100.0)), 4)

        metadata = dict(result)
        metadata.pop("symbol", None)
        metadata["impulse_lookback"] = lookback
        metadata["latest_unmitigated"] = latest
        return FeatureResult(self.name, signal, confidence, metadata)
