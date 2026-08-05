"""Liquidity feature.

Assesses tradability from the order book: bid/ask spread, resting depth and
bid/ask imbalance. A market-data feature; degrades to neutral when the book
cannot be fetched or the feature runs in df-only mode.
"""

from typing import Any

from agent.features.base import BaseFeature, FeatureResult


class LiquidityFeature(BaseFeature):
    name = "liquidity"
    df_only = False
    cache_ttl = 60.0

    def _load_config(self) -> None:
        super()._load_config()
        cfg = self.config.get("features", {}).get(self.name, {}) or {}
        risk = self.config.get("risk", {}) or {}
        self.book_depth = int(cfg.get("orderbook_depth", int(cfg.get("depth", 10))))
        self.max_spread_pct = float(
            cfg.get("max_spread_pct", float(risk.get("min_fee_tolerance_pct", 0.15)))
        )

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        if self.market_data is None:
            return self.unavailable()
        try:
            book = self.market_data.fetch_order_book(symbol, self.book_depth)
        except Exception:
            return self.unavailable()
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return FeatureResult.neutral(self.name, metadata={"spread_pct": None})

        bid = float(bids[0][0])
        ask = float(asks[0][0])
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0 if mid else 0.0
        bid_amt = sum(float(p[1]) for p in bids)
        ask_amt = sum(float(p[1]) for p in asks)
        imbalance = (bid_amt - ask_amt) / (bid_amt + ask_amt) if (bid_amt + ask_amt) else 0.0
        bid_notional = sum(float(p[0]) * float(p[1]) for p in bids)
        ask_notional = sum(float(p[0]) * float(p[1]) for p in asks)

        if spread_pct <= self.max_spread_pct:
            signal, confidence = "liquid", min(0.95, 0.5 + (self.max_spread_pct - spread_pct) * 2.0)
        else:
            signal, confidence = "illiquid", min(0.95, 0.5 + (spread_pct - self.max_spread_pct) * 2.0)

        metadata = {
            "spread_pct": round(spread_pct, 4),
            "bid_depth_usdt": round(bid_notional, 2),
            "ask_depth_usdt": round(ask_notional, 2),
            "imbalance": round(imbalance, 4),
            "cost_ok": spread_pct <= self.max_spread_pct,
        }
        return FeatureResult(self.name, signal, confidence, metadata)
