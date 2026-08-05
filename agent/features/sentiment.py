"""Sentiment feature.

Derives crowd / smart-money sentiment from the same signals the old
``SmartMoneyAnalyzer`` used (order-book imbalance, CVD buy pressure, OBV).
Metadata preserves the exact legacy result shape (``book``/``cvd``/``obv``/
``score``) so the screener's ``smart_money`` field stays byte-compatible.
"""

from typing import Any, Optional

from agent.core.smart_money import SmartMoneyAnalyzer
from agent.features.base import BaseFeature, FeatureResult

_DIRECTION_TO_SIGNAL = {"LONG": "bullish", "SHORT": "bearish", "NEUTRAL": "neutral"}


class SentimentFeature(BaseFeature):
    name = "sentiment"
    df_only = False
    cache_ttl = 300.0

    def _load_config(self) -> None:
        super()._load_config()
        self._analyzer: Optional[SmartMoneyAnalyzer] = None
        if self.market_data is not None:
            self._analyzer = SmartMoneyAnalyzer(self.market_data, self.config)

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        if self._analyzer is None:
            return self.unavailable()
        try:
            result = self._analyzer.analyze(symbol, df)
        except Exception:
            return self.unavailable()
        if not result:
            return self.unavailable()

        score = result.get("score") or {}
        direction = score.get("direction") or "NEUTRAL"
        raw_score = float(score.get("score") or 0)
        signal = _DIRECTION_TO_SIGNAL.get(direction, "neutral")
        confidence = min(0.95, 0.5 + abs(raw_score) * 0.15) if signal != "neutral" else 0.5
        return FeatureResult(self.name, signal, confidence, result)
