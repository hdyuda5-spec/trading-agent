"""Deterministic Fair Value Gap (FVG) detector.

Pure-algorithm SMC-style FVG detection over a candle series — no machine
learning, no LLM, no subjective drawings.

Definitions (all deterministic):

- Bullish FVG
  Candle ``i+1`` gaps up away from candle ``i-1``: ``low[i+1] > high[i-1]``.
  The imbalance zone is ``[high[i-1], low[i+1]]`` — a volume vacuum that
  price often revisits.

- Bearish FVG
  Candle ``i+1`` gaps down away from candle ``i-1``: ``high[i+1] < low[i-1]``.
  The imbalance zone is ``[high[i+1], low[i-1]]``.

- Unmitigated FVG
  No later bar has traded back into the zone (``filled_ratio`` = 0.0).

- Mitigated FVG
  A later bar has traded *into* the zone without fully traversing it
  (partial fill; ``filled_ratio`` in (0, 1)).

- Filled FVG
  A later bar has fully traversed the zone (``filled_ratio`` = 1.0).

- Invalidated FVG
  A later bar has *closed* beyond the far side of the zone: a bullish FVG is
  invalidated when price closes below its low; a bearish FVG when price
  closes above its high. Invalidation subsumes a full fill.

Gap size is configurable as a minimum percentage of the zone midpoint price
(``min_gap_pct``), which filters out micro-gaps.

Multi-timeframe support: ``analyze`` handles one dataframe;
``analyze_timeframes(symbol, {tf: df, ...})`` runs the same deterministic
analysis over each timeframe and labels every gap with its timeframe.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class Config:
    min_gap_pct: float = 0.0
    min_bars: int = 5
    max_gaps: int = 100
    impulse_lookback: int = 6
    timeframe: str = "default"


def _bars(df: Any) -> tuple:
    return (
        df["open"].to_numpy(dtype=float),
        df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
        df["volume"].to_numpy(dtype=float),
    )


def _classify(direction: str, zone_low: float, zone_high: float, later_lows: Any, later_highs: Any, later_closes: Any) -> tuple:
    """Return ``(status, filled, filled_ratio)`` for one gap."""
    if later_lows.size == 0:
        return "unmitigated", False, 0.0
    if direction == "bullish":
        if (later_closes < zone_low).any():
            return "invalidated", True, 1.0
        min_low = float(later_lows.min())
        if min_low <= zone_low:
            return "filled", True, 1.0
        if min_low <= zone_high:
            return "mitigated", False, float(round((zone_high - min_low) / (zone_high - zone_low), 4))
        return "unmitigated", False, 0.0
    if (later_closes > zone_high).any():
        return "invalidated", True, 1.0
    max_high = float(later_highs.max())
    if max_high >= zone_high:
        return "filled", True, 1.0
    if max_high >= zone_low:
        return "mitigated", False, float(round((max_high - zone_low) / (zone_high - zone_low), 4))
    return "unmitigated", False, 0.0


def detect_fvgs(df: Any, cfg: Config) -> List[dict]:
    """Detect all fair value gaps, most recent last."""
    _, high, low, close, _ = _bars(df)
    n = len(df)
    gaps: List[dict] = []
    for i in range(1, n - 1):
        if i + 1 >= n:
            break
        if low[i + 1] > high[i - 1]:
            zone_low, zone_high = high[i - 1], low[i + 1]
            direction = "bullish"
        elif high[i + 1] < low[i - 1]:
            zone_low, zone_high = high[i + 1], low[i - 1]
            direction = "bearish"
        else:
            continue
        gap = zone_high - zone_low
        if gap <= 0:
            continue
        if gap < ((zone_high + zone_low) / 2.0) * cfg.min_gap_pct / 100.0:
            continue
        status, filled, ratio = _classify(
            direction, zone_low, zone_high,
            low[i + 2 : n], high[i + 2 : n], close[i + 2 : n],
        )
        gaps.append(
            {
                "direction": direction,
                "high": round(float(zone_high), 8),
                "low": round(float(zone_low), 8),
                "filled": filled,
                "status": status,
                "filled_ratio": float(ratio),
                "gap": round(float(gap), 8),
                "index": i,
            }
        )
    if cfg.max_gaps > 0 and len(gaps) > cfg.max_gaps:
        gaps = gaps[-cfg.max_gaps:]
    return gaps


def neutral_result(symbol: str, timeframe: str, reason: str) -> dict:
    """Fail-open result when the input is insufficient or invalid."""
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "fvg": [],
        "count": 0,
        "reason": reason,
    }


class FairValueGapAnalyzer:
    """Deterministic fair value gap detector.

    ``analyze(symbol, df)`` returns ``{"symbol", "timeframe", "fvg", "count",
    "reason"}`` with every gap stamped with its timeframe. It never raises:
    invalid input yields ``neutral_result``.
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = (config or {}).get("fvg") or (config or {}).get("fair_value_gap") or {}
        self.cfg = Config(
            min_gap_pct=float(cfg.get("min_gap_pct", 0.0)),
            min_bars=int(cfg.get("min_bars", 5)),
            max_gaps=int(cfg.get("max_gaps", 100)),
            impulse_lookback=int(cfg.get("impulse_lookback", 6)),
            timeframe=str(cfg.get("timeframe", "default")),
        )

    def analyze(self, symbol: str, df: Any, timeframe: Optional[str] = None) -> dict:
        tf = timeframe or self.cfg.timeframe
        try:
            if df is None or len(df) < self.cfg.min_bars:
                return neutral_result(symbol, tf, "insufficient_data")
            gaps = detect_fvgs(df, self.cfg)
            for g in gaps:
                g["timeframe"] = tf
            return {"symbol": symbol, "timeframe": tf, "fvg": gaps, "count": len(gaps), "reason": None}
        except Exception:
            return neutral_result(symbol, tf, "analysis_failed")

    def analyze_timeframes(self, symbol: str, timeframes: Dict[str, Any]) -> dict:
        """Run the same analysis over several timeframes: ``{tf: result}``."""
        return {tf: self.analyze(symbol, df, timeframe=tf) for tf, df in timeframes.items()}
