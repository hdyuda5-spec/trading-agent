"""Open Interest feature.

Reads the perpetual futures open interest as a participation/conviction
signal. Direction is derived from how OI moves *relative to price*:

- OI rising + price rising  -> new longs entering    -> bullish
- OI rising + price falling -> new shorts entering   -> bearish
- OI falling                -> positions unwinding   -> weak counter-signal

Data comes from the underlying ccxt client (``fetch_open_interest`` /
``fetch_open_interest_history``); no exchange integration is modified. The
feature degrades to neutral whenever the market does not expose OI.
"""

from typing import Any, List, Optional

from agent.features.base import BaseFeature, FeatureResult


class OpenInterestFeature(BaseFeature):
    name = "open_interest"
    df_only = False
    opt_in = True
    cache_ttl = 300.0

    def _load_config(self) -> None:
        super()._load_config()
        cfg = self.config.get("features", {}).get(self.name, {}) or {}
        self.timeframe = str(cfg.get("timeframe", "1h"))
        self.history_limit = int(cfg.get("history_limit", 10))
        self.rise_threshold = float(cfg.get("rise_threshold", 0.05))
        self.fall_threshold = float(cfg.get("fall_threshold", 0.05))
        self.price_window = int(cfg.get("price_window", 10))

    def _client(self):
        return getattr(self.market_data, "client", None)

    def _fetch_current(self, symbol: str) -> Optional[float]:
        client = self._client()
        if client is None or not callable(getattr(client, "fetch_open_interest", None)):
            return None
        try:
            row = client.fetch_open_interest(symbol)
        except Exception:
            return None
        if not row:
            return None
        amt = row.get("openInterestAmount") or row.get("openInterestValue")
        try:
            return float(amt) if amt is not None else None
        except (TypeError, ValueError):
            return None

    def _fetch_history(self, symbol: str) -> Optional[List[float]]:
        client = self._client()
        if client is None or not callable(getattr(client, "fetch_open_interest_history", None)):
            return None
        try:
            rows = client.fetch_open_interest_history(symbol, self.timeframe, limit=self.history_limit)
        except Exception:
            return None
        out = []
        for row in rows or []:
            amt = row.get("openInterestAmount") or row.get("openInterestValue")
            if amt is not None:
                try:
                    out.append(float(amt))
                except (TypeError, ValueError):
                    continue
        return out if out else None

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        if self.market_data is None:
            return self.unavailable()

        history = self._fetch_history(symbol)
        current = self._fetch_current(symbol)
        if current is None and history:
            current = history[-1]
        if current is None:
            return self.unavailable()

        close = df["close"]
        n = len(df)
        if n > 1:
            base = int(max(0, n - self.price_window))
            price_chg = float(close.iloc[-1] / close.iloc[base] - 1)
        else:
            price_chg = 0.0

        oi_chg = None
        if history and len(history) >= 2:
            baseline = sum(history[:-1]) / len(history[:-1]) or current
            oi_chg = (current - baseline) / baseline if baseline else 0.0

        metadata = {
            "open_interest": round(current, 4),
            "oi_change_pct": round(oi_chg * 100.0, 3) if oi_chg is not None else None,
            "price_change_pct": round(price_chg * 100.0, 3),
            "history_samples": len(history) if history else 0,
        }

        if oi_chg is None:
            signal, confidence = "neutral", 0.5
        elif oi_chg >= self.rise_threshold:
            magnitude = min(abs(oi_chg), 0.30)
            signal = "bullish" if price_chg >= 0 else "bearish"
            confidence = min(0.90, 0.5 + magnitude * 1.2)
        elif oi_chg <= -self.fall_threshold:
            magnitude = min(abs(oi_chg), 0.25)
            signal = "bearish" if price_chg >= 0 else "bullish"
            confidence = min(0.80, 0.5 + magnitude * 1.0)
        else:
            signal, confidence = "neutral", 0.5

        return FeatureResult(self.name, signal, confidence, metadata)
