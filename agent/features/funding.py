"""Funding feature.

Reads the perpetual futures funding rate as a crowd-positioning signal:

- strongly positive funding → longs are crowded → contrarian bearish
- strongly negative funding → shorts are crowded → contrarian bullish

Degrades to neutral when the exchange does not expose a funding rate.
"""

from typing import Any

from agent.features.base import BaseFeature, FeatureResult


class FundingFeature(BaseFeature):
    name = "funding"
    df_only = False
    cache_ttl = 3600.0

    def _load_config(self) -> None:
        super()._load_config()
        cfg = self.config.get("features", {}).get(self.name, {}) or {}
        self.extreme_rate = float(cfg.get("extreme_rate", 0.0001))

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        if self.market_data is None or not callable(getattr(self.market_data, "fetch_funding_rate", None)):
            return self.unavailable()
        try:
            rate = self.market_data.fetch_funding_rate(symbol)
        except Exception:
            return self.unavailable()
        if rate is None:
            return self.unavailable()
        rate = float(rate)
        annualized_pct = rate * 24 * 365 * 100.0

        if rate >= self.extreme_rate:
            signal, confidence = "bearish", min(0.9, 0.5 + (rate / self.extreme_rate) * 0.3)
        elif rate <= -self.extreme_rate:
            signal, confidence = "bullish", min(0.9, 0.5 + (abs(rate) / self.extreme_rate) * 0.3)
        else:
            signal, confidence = "neutral", 0.5

        metadata = {"funding_rate": round(rate, 8), "annualized_pct": round(annualized_pct, 4)}
        return FeatureResult(self.name, signal, confidence, metadata)
