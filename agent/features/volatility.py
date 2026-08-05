"""Volatility feature.

Interprets volatility from ATR (as % of price) and Bollinger Band width.
The Bollinger values live in metadata so mean-reversion strategies can
consume the bands without re-reading OHLCV.
"""

from typing import Any

from agent.core.utils import compute_atr, is_valid_atr
from agent.features.base import BaseFeature, FeatureResult


class VolatilityFeature(BaseFeature):
    name = "volatility"
    df_only = True

    def _load_config(self) -> None:
        super()._load_config()
        cfg = self.config.get("features", {}).get(self.name, {}) or {}
        risk = self.config.get("risk", {}) or {}
        self.atr_period = int(cfg.get("atr_period", int(risk.get("atr_period", 14))))
        self.bb_period = int(cfg.get("bb_period", 20))
        self.bb_std = float(cfg.get("bb_std", 2))
        self.normal_pct = float(cfg.get("normal_pct", float(risk.get("atr_normal_pct", 1.0))))
        self.high_mult = float(cfg.get("high_mult", 1.3))
        self.low_mult = float(cfg.get("low_mult", 0.7))

    @staticmethod
    def bollinger(series: Any, period: int = 20, std: float = 2.0):
        sma = series.rolling(period).mean()
        sd = series.rolling(period).std()
        return sma, sma + std * sd, sma - std * sd

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        n = len(df)
        if n < max(self.atr_period + 1, self.bb_period + 1, 3):
            return FeatureResult.neutral(self.name, metadata={"reason": "insufficient_data"})

        close = df["close"]
        price = float(close.iloc[-1])
        atr_val = float(compute_atr(df, self.atr_period).iloc[-1])
        atr_pct = atr_val / price * 100.0 if price else 0.0

        sma, upper, lower = self.bollinger(close, self.bb_period, self.bb_std)
        bb_sma = float(sma.iloc[-1]) if len(sma) else None
        bb_upper = float(upper.iloc[-1]) if len(upper) else None
        bb_lower = float(lower.iloc[-1]) if len(lower) else None
        width_pct = (bb_upper - bb_lower) / bb_sma * 100.0 if (bb_sma and bb_upper is not None and bb_lower is not None) else 0.0

        if not is_valid_atr(atr_val) or atr_pct <= 0 or self.normal_pct <= 0:
            signal, confidence = "neutral", 0.5
        else:
            ratio = atr_pct / self.normal_pct
            if ratio >= self.high_mult:
                signal, confidence = "high", min(0.95, 0.5 + (ratio - 1.0) * 0.3)
            elif ratio <= self.low_mult:
                signal, confidence = "low", min(0.95, 0.5 + (1.0 / ratio - 1.0) * 0.3)
            else:
                signal, confidence = "neutral", 0.5

        metadata = {
            "atr": round(atr_val, 8),
            "atr_pct": round(atr_pct, 4),
            "bb_period": self.bb_period,
            "bb_std": self.bb_std,
            "bb_sma": round(bb_sma, 8) if bb_sma is not None else None,
            "bb_upper": round(bb_upper, 8) if bb_upper is not None else None,
            "bb_lower": round(bb_lower, 8) if bb_lower is not None else None,
            "bb_width_pct": round(width_pct, 4),
        }
        return FeatureResult(self.name, signal, confidence, metadata)
