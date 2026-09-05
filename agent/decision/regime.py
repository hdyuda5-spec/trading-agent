"""Market Regime Engine (Phase 11).

Regime is classified from the candle stack only (no network, no assumptions):

    TRENDING_UP      strong ADX trend + price above mid EMA
    TRENDING_DOWN    strong ADX trend + price below mid EMA
    RANGING          weak ADX, meaningful range
    HIGH_VOLATILITY  ATR% above the configured high-vol threshold (overrides
                     direction labels — safety first)
    LOW_VOLATILITY   ATR% below half the threshold
    UNCERTAIN        not enough data / contradictory signals

Strategy-selection hints and a ``size_multiplier`` are emitted so downstream
layers (risk/strategy) can adapt without re-implementing regime logic.
"""

from typing import Optional

from agent.core.utils import compute_adx, compute_atr, compute_ema, is_valid_atr

TRENDING_UP = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"
RANGING = "RANGING"
HIGH_VOLATILITY = "HIGH_VOLATILITY"
LOW_VOLATILITY = "LOW_VOLATILITY"
UNCERTAIN = "UNCERTAIN"

SIZE_MULTIPLIERS = {
    TRENDING_UP: 1.0,
    TRENDING_DOWN: 1.0,
    RANGING: 0.75,
    HIGH_VOLATILITY: 0.5,
    LOW_VOLATILITY: 1.0,
    UNCERTAIN: 0.0,
}

_TREND_MOMENTUM = {
    TRENDING_UP: "momentum",
    TRENDING_DOWN: "momentum",
    RANGING: "mean_reversion",
    HIGH_VOLATILITY: None,
    LOW_VOLATILITY: None,
    UNCERTAIN: None,
}


class MarketRegimeEngine:
    def __init__(self, config=None):
        self.config = config or {}
        regime_cfg = (self.config.get("decision", {}) or {}).get("regime", {}) or {}
        self.high_vol_atr_pct = float(regime_cfg.get("high_vol_atr_pct", 2.0))
        self.adx_min = float(regime_cfg.get("adx_min", 20.0))
        self.atr_period = int(self.config.get("risk", {}).get("atr_period", 14) or 14)

    def detect(self, df, atr: Optional[float] = None) -> dict:
        if df is None or len(df) < 60:
            return {"regime": UNCERTAIN, "label": "Data kurang", "size_multiplier": 0.0,
                    "momentum": None, "atr_pct": None, "adx": None, "reason": "insufficient_data"}

        last = float(df["close"].iloc[-1])
        atr_val = atr
        if not is_valid_atr(atr_val):
            try:
                atr_val = float(compute_atr(df, self.atr_period).iloc[-1])
            except Exception:
                atr_val = None
        atr_pct = (atr_val / last * 100.0) if is_valid_atr(atr_val) and last > 0 else None

        high_vol = atr_pct is not None and atr_pct >= self.high_vol_atr_pct
        low_vol = atr_pct is not None and atr_pct < self.high_vol_atr_pct / 2.0

        ema = compute_ema(df["close"], 50)
        price_above = last >= float(ema.iloc[-1]) if len(ema) and not _nan(ema.iloc[-1]) else None

        try:
            adx = float(compute_adx(df, 14).iloc[-1])
        except Exception:
            adx = None
        trending = adx is not None and not _nan(adx) and adx >= self.adx_min

        if high_vol:
            return {"regime": HIGH_VOLATILITY, "size_multiplier": SIZE_MULTIPLIERS[HIGH_VOLATILITY],
                    "momentum": None, "atr_pct": atr_pct, "adx": adx,
                    "reason": f"atr {atr_pct:.2f}% >= {self.high_vol_atr_pct}%"}
        if not trending or price_above is None:
            if low_vol:
                return {"regime": LOW_VOLATILITY, "size_multiplier": SIZE_MULTIPLIERS[LOW_VOLATILITY],
                        "momentum": None, "atr_pct": atr_pct, "adx": adx,
                        "reason": f"adx {adx if adx is not None else 'n/a'} < {self.adx_min}"}
            return {"regime": RANGING, "size_multiplier": SIZE_MULTIPLIERS[RANGING],
                    "momentum": _TREND_MOMENTUM[RANGING], "atr_pct": atr_pct, "adx": adx,
                    "reason": f"adx {adx if adx is not None else 'n/a'} < {self.adx_min}"}
        regime = TRENDING_UP if price_above else TRENDING_DOWN
        return {"regime": regime, "size_multiplier": SIZE_MULTIPLIERS[regime],
                "momentum": _TREND_MOMENTUM[regime], "atr_pct": atr_pct, "adx": adx,
                "reason": f"adx {adx:.1f} >= {self.adx_min}, price {'above' if price_above else 'below'} EMA50"}


def _nan(value) -> bool:
    try:
        import math

        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True