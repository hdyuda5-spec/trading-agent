"""Deterministic Premium / Discount Zone analyzer.

Pure-algorithm SMC-style premium-discount analysis over a candle series — no
machine learning, no LLM, no heuristics. It answers one question: where does
the last close sit relative to the latest confirmed swing range?

Definitions (all deterministic):

- Latest swing range
  The most recent *confirmed* fractal swing high and the most recent
  *confirmed* fractal swing low within the ``lookback`` window. Confirmation
  uses the shared fractal definition (bar ``i`` is a swing high when
  ``high[i]`` strictly exceeds the ``strength`` highs on both sides, and only
  when ``i + strength < len(df)`` — non-repainting). If either side has no
  confirmed swing inside the window, the analysis degrades to neutral.

- Equilibrium (EQ)
  The midpoint of the range: ``(swing_high + swing_low) / 2``.

- Discount
  How far the last close has travelled *up* from the swing low, as a fraction
  of the range: ``discount = (price - swing_low) / range``. Clamped to [0, 1].

- Premium
  The complement: ``premium = (swing_high - price) / range``. Always
  ``premium + discount == 1.0``.

- Zone
  ``"discount"`` when price is below EQ (undervalued — SMC prefers buys here),
  ``"premium"`` when above EQ (overvalued — sells), ``"equilibrium"`` when
  price sits on EQ within ``equilibrium_tolerance_pct`` of the EQ price, and
  ``"unknown"`` when the analysis degraded to neutral.

Example — swing low 10.5, swing high 15.5 (range 5.0, EQ 13.0), last close
11.9::

    discount = (11.9 - 10.5) / 5.0 = 0.28
    premium  = 0.72
    zone     = "discount"

Multi-timeframe support mirrors the other analyzers.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from agent.features.order_block.analyzer import find_swings


@dataclass
class Config:
    pivot_strength: int = 2
    lookback: int = 100
    min_bars: int = 16
    equilibrium_tolerance_pct: float = 0.0
    timeframe: str = "default"


def _bars(df: Any) -> tuple:
    return (
        df["open"].to_numpy(dtype=float),
        df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
        df["volume"].to_numpy(dtype=float),
    )


def latest_swings(df: Any, strength: int = 2, lookback: int = 100) -> dict:
    """Most recent confirmed swing high/low within the lookback window."""
    last = len(df) - 1
    swings = find_swings(df, strength)
    window_start = last - lookback
    highs = [s for s in swings["high"] if s["confirmed"] and s["index"] >= window_start]
    lows = [s for s in swings["low"] if s["confirmed"] and s["index"] >= window_start]
    if not highs or not lows:
        return {}
    return {
        "swing_high": float(max(highs, key=lambda s: s["index"])["price"]),
        "swing_low": float(max(lows, key=lambda s: s["index"])["price"]),
        "swing_high_index": int(max(highs, key=lambda s: s["index"])["index"]),
        "swing_low_index": int(max(lows, key=lambda s: s["index"])["index"]),
    }


def analyze_premium_discount(df: Any, cfg: Config) -> dict:
    """Pure function returning the premium/discount result dict (or neutral)."""
    _, _, _, close, _ = _bars(df)
    last = len(df) - 1
    range_info = latest_swings(df, cfg.pivot_strength, cfg.lookback)
    if not range_info:
        return {"neutral": "no_recent_swing_range"}

    swing_high = range_info["swing_high"]
    swing_low = range_info["swing_low"]
    span = swing_high - swing_low
    if span <= 0:
        return {"neutral": "no_recent_swing_range"}

    eq = (swing_high + swing_low) / 2.0
    price = float(close[last])
    position = (price - swing_low) / span
    discount = round(min(max(position, 0.0), 1.0), 4)
    premium = round(min(max(1.0 - position, 0.0), 1.0), 4)

    tol = eq * cfg.equilibrium_tolerance_pct / 100.0
    if abs(price - eq) <= tol:
        zone = "equilibrium"
    elif price < eq:
        zone = "discount"
    else:
        zone = "premium"

    return {
        "premium": premium,
        "discount": discount,
        "zone": zone,
        "equilibrium": round(eq, 8),
        "swing_high": round(swing_high, 8),
        "swing_low": round(swing_low, 8),
        "swing_high_index": range_info["swing_high_index"],
        "swing_low_index": range_info["swing_low_index"],
        "price": round(price, 8),
        "range": round(span, 8),
        "position": round(position, 4),
        "in_range": bool(swing_low <= price <= swing_high),
    }


def neutral_result(symbol: str, timeframe: str, reason: str) -> dict:
    """Fail-open result when the input is insufficient or invalid."""
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "premium": None,
        "discount": None,
        "zone": "unknown",
        "equilibrium": None,
        "swing_high": None,
        "swing_low": None,
        "swing_high_index": None,
        "swing_low_index": None,
        "price": None,
        "range": None,
        "position": None,
        "in_range": False,
        "reason": reason,
    }


class PremiumDiscountAnalyzer:
    """Deterministic premium/discount zone analyzer.

    ``analyze(symbol, df)`` returns ``{"premium", "discount", "zone", ...}``
    using the latest confirmed swing high and low. It never raises: invalid
    input yields a neutral result with ``zone="unknown"``.
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = (config or {}).get("premium_discount") or (config or {}).get("premium_discount_analyzer") or {}
        self.cfg = Config(
            pivot_strength=int(cfg.get("pivot_strength", 2)),
            lookback=int(cfg.get("lookback", 100)),
            min_bars=int(cfg.get("min_bars", 16)),
            equilibrium_tolerance_pct=float(cfg.get("equilibrium_tolerance_pct", 0.0)),
            timeframe=str(cfg.get("timeframe", "default")),
        )

    def analyze(self, symbol: str, df: Any, timeframe: Optional[str] = None) -> dict:
        tf = timeframe or self.cfg.timeframe
        try:
            if df is None or len(df) < self.cfg.min_bars:
                return neutral_result(symbol, tf, "insufficient_data")
            result = analyze_premium_discount(df, self.cfg)
            if "neutral" in result:
                return neutral_result(symbol, tf, result["neutral"])
            return {
                "symbol": symbol,
                "timeframe": tf,
                "reason": None,
                **result,
            }
        except Exception:
            return neutral_result(symbol, tf, "analysis_failed")

    def analyze_timeframes(self, symbol: str, timeframes: Dict[str, Any]) -> dict:
        """Run the same analysis over several timeframes: ``{tf: result}``."""
        return {tf: self.analyze(symbol, df, timeframe=tf) for tf, df in timeframes.items()}
