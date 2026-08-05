"""Trend feature.

Wraps the EMA/RSI/ADX indicators already implemented in ``agent.core.utils``
into a structured, immutable feature. Two directional modes are supported:

- ``ema_cross`` (default): signal from a fast/slow EMA pair, e.g. 21/50.
- ``ema_stack``: signal when EMA9 > EMA21 > EMA50 (bullish) or the inverse.

Metadata always exposes the raw EMA9/21/50, RSI, ADX and crossover *events*
so consumers (e.g. the momentum strategy) can act on transitions without
re-reading raw OHLCV.
"""

from typing import Any, Optional

from agent.core.utils import compute_adx, compute_ema, compute_rsi
from agent.features.base import BaseFeature, FeatureResult


class TrendFeature(BaseFeature):
    name = "trend"
    df_only = True

    def _load_config(self) -> None:
        super()._load_config()
        cfg = self.config.get("features", {}).get(self.name, {}) or {}
        risk = self.config.get("risk", {}) or {}
        self.mode = cfg.get("mode", "ema_cross")
        self.ema_fast = int(cfg.get("ema_fast", 21))
        self.ema_slow = int(cfg.get("ema_slow", 50))
        self.adx_enabled = bool(cfg.get("adx_enabled", False))
        self.adx_period = int(cfg.get("adx_period", int(risk.get("atr_period", 14))))
        self.min_adx = float(cfg.get("min_adx", 20))

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        close = df["close"]
        n = len(df)
        if n < 2:
            return FeatureResult.neutral(
                self.name, metadata={"price": float(close.iloc[-1]) if n else None, "reason": "insufficient_data"}
            )

        price = float(close.iloc[-1])
        ema9 = compute_ema(close, 9)
        ema21 = compute_ema(close, 21)
        ema50 = compute_ema(close, 50)
        rsi = compute_rsi(close, 14)
        emas = {9: ema9, 21: ema21, 50: ema50}
        if self.ema_fast not in emas:
            emas[self.ema_fast] = compute_ema(close, self.ema_fast)
        if self.ema_slow not in emas:
            emas[self.ema_slow] = compute_ema(close, self.ema_slow)

        def _val(series: Any) -> Optional[float]:
            try:
                return float(series.iloc[-1])
            except (IndexError, TypeError, ValueError):
                return None

        cur = {p: _val(emas[p]) for p in emas}
        prev = {p: _val(emas[p].iloc[:-1] if len(emas[p]) > 1 else emas[p]) for p in emas}

        def cross_state(fast_p: int, slow_p: int) -> Optional[str]:
            f, s = cur.get(fast_p), cur.get(slow_p)
            if f is None or s is None:
                return None
            if f > s:
                return "bullish"
            if f < s:
                return "bearish"
            return None

        def cross_event(fast_p: int, slow_p: int) -> Optional[str]:
            pf, ps = prev.get(fast_p), prev.get(slow_p)
            f, s = cur.get(fast_p), cur.get(slow_p)
            if None in (pf, ps, f, s):
                return None
            if pf <= ps and f > s:
                return "bullish"
            if pf >= ps and f < s:
                return "bearish"
            return None

        if self.mode == "ema_stack":
            e9, e21, e50 = cur.get(9), cur.get(21), cur.get(50)
            if e9 is not None and e21 is not None and e50 is not None:
                if e9 > e21 > e50:
                    signal = "bullish"
                elif e9 < e21 < e50:
                    signal = "bearish"
                else:
                    signal = "neutral"
            else:
                signal = "neutral"
        else:
            signal = cross_state(self.ema_fast, self.ema_slow) or "neutral"

        fast_val = cur.get(self.ema_fast)
        slow_val = cur.get(self.ema_slow)
        if self.mode == "ema_stack":
            fast_val = fast_val if fast_val is not None else cur.get(21)
            slow_val = slow_val if slow_val is not None else cur.get(50)

        adx = None
        if n >= self.adx_period * 2:
            try:
                adx = float(compute_adx(df, self.adx_period).iloc[-1])
                if adx != adx:  # NaN guard
                    adx = None
            except Exception:
                adx = None

        confidence = self._confidence(signal, price, cur, fast_val, slow_val, adx)

        metadata = {
            "price": round(price, 8) if price else None,
            "ema9": round(cur.get(9) or 0.0, 8),
            "ema21": round(cur.get(21) or 0.0, 8),
            "ema50": round(cur.get(50) or 0.0, 8),
            "ema_fast": round(fast_val or 0.0, 8),
            "ema_slow": round(slow_val or 0.0, 8),
            "rsi": round(rsi.iloc[-1], 2) if len(rsi) else None,
            "adx": round(adx, 2) if adx is not None else None,
            "mode": self.mode,
            "ema_fast_period": self.ema_fast,
            "ema_slow_period": self.ema_slow,
            "cross_9_21": cross_state(9, 21),
            "cross_21_50": cross_state(21, 50),
            "cross_9_21_event": cross_event(9, 21),
            "cross_21_50_event": cross_event(21, 50),
        }
        return FeatureResult(self.name, signal, confidence, metadata)

    def _confidence(
        self,
        signal: str,
        price: float,
        cur: dict,
        fast_val: Optional[float],
        slow_val: Optional[float],
        adx: Optional[float],
    ) -> float:
        if signal == "neutral" or not fast_val or not slow_val or not price:
            return 0.5
        spread = abs(fast_val - slow_val) / price
        confidence = min(0.95, 0.55 + spread * 40.0)
        if self.adx_enabled and adx is not None and adx >= self.min_adx:
            confidence += 0.1
        return min(0.97, confidence)
