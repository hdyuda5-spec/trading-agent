"""Whale feature.

Structured view of large-trader (whale) flow, reusing the pure aggregation
in ``agent.core.whale.aggregate_whale_flow`` so it stays identical to the
``WhaleDetector`` used for trade filtering.
"""

from typing import Any

from agent.core.whale import aggregate_whale_flow
from agent.features.base import BaseFeature, FeatureResult


class WhaleFeature(BaseFeature):
    name = "whale"
    df_only = False
    cache_ttl = 300.0

    def _load_config(self) -> None:
        super()._load_config()
        wcfg = self.config.get("whale", {}) or {}
        fcfg = self.config.get("features", {}).get(self.name, {}) or {}
        self.window_seconds = int(fcfg.get("window_minutes", wcfg.get("window_minutes", 30))) * 60
        self.min_notional = float(fcfg.get("min_notional_usdt", wcfg.get("min_notional_usdt", 50000)))

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        if self.market_data is None:
            return self.unavailable()
        try:
            trades = self.market_data.fetch_trades(symbol, limit=1000)
        except Exception:
            return self.unavailable()
        event = aggregate_whale_flow(trades, self.window_seconds, self.min_notional)
        if not event:
            return FeatureResult.neutral(self.name, metadata={"n": 0, "net_usdt": 0.0})

        net = event["net_usdt"]
        if net > 0:
            signal, confidence = "bullish", min(0.9, 0.5 + abs(net) / max(self.min_notional, 1.0) * 0.3)
        elif net < 0:
            signal, confidence = "bearish", min(0.9, 0.5 + abs(net) / max(self.min_notional, 1.0) * 0.3)
        else:
            signal, confidence = "neutral", 0.5

        metadata = dict(event)
        metadata.pop("symbol", None)
        return FeatureResult(self.name, signal, confidence, metadata)
