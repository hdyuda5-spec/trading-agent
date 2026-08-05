"""Deterministic Order Block analyzer.

Pure-algorithm SMC-style order-block detection over a candle series — no
machine learning, no LLM, and crucially *no heuristics based on candle
colors*. A block is defined purely by price structure (confirmed fractal
swings) and leg size relative to ATR.

Definitions (all deterministic):

- Swing High / Swing Low
  Fractal pivots: bar ``i`` is a swing high when ``high[i]`` strictly
  exceeds the ``strength`` highs before and after it (mirror for lows). A
  pivot is *confirmed* only when the full right window exists
  (``i + strength < len(df)``); trailing pivots are never used. This is what
  makes the output non-repainting.

- Impulse leg
  An adjacent confirmed swing pair whose move is at least
  ``impulse_min_atr * ATR`` (measured with ATR at the leg end): a low-to-high
  pair with ``high > low`` is a bullish leg; a high-to-low pair with
  ``low < high`` is a bearish leg.

- Bullish Order Block
  The *origin candle* of a bullish leg — the candle at the leg's confirmed
  swing low. Zone = the full ``[low, high]`` range of that candle. This is
  where buy orders that started the impulse are assumed to sit.

- Bearish Order Block
  The origin candle of a bearish leg — the candle at the leg's confirmed
  swing high. Zone = that candle's ``[low, high]`` range.

- Mitigated Block
  A later bar has traded back into the block's zone (partial or full
  re-entry; ``filled_ratio`` in (0, 1]).

- Invalid Block
  A later bar has *closed* beyond the far side of the zone (a bullish block
  closes below its low; a bearish block closes above its high) — the block
  is negated.

- Breaker Block
  An invalidated block whose polarity flipped: after the invalidating close,
  a later bar *closed* back through the block in the original direction
  (a broken bullish block reclaims above its high). The block now acts as a
  reversed (breaker) zone.

Blocks are ranked by a deterministic quality score and assigned a ``rank``
(1 = best) plus a ``quality`` tier.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from agent.core.utils import compute_atr


@dataclass
class Config:
    pivot_strength: int = 2
    impulse_min_atr: float = 1.0
    atr_period: int = 14
    max_lookback: int = 60
    max_blocks: int = 100
    min_bars: int = 16
    timeframe: str = "default"


def _bars(df: Any) -> tuple:
    return (
        df["open"].to_numpy(dtype=float),
        df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
        df["volume"].to_numpy(dtype=float),
    )


def find_swings(df: Any, strength: int = 2) -> dict:
    """Confirmed + candidate fractal swing highs/lows (non-repainting)."""
    _, high, low, _, _ = _bars(df)
    n = len(df)
    highs: List[dict] = []
    lows: List[dict] = []
    for i in range(strength, n):
        confirmed = i + strength < n
        left_ok = i - strength >= 0
        if left_ok and all(high[i] > high[i - k] for k in range(1, strength + 1)):
            if confirmed and all(high[i] > high[i + k] for k in range(1, strength + 1)):
                highs.append({"index": i, "price": float(high[i]), "confirmed": True})
            elif not confirmed:
                highs.append({"index": i, "price": float(high[i]), "confirmed": False})
        if left_ok and all(low[i] < low[i - k] for k in range(1, strength + 1)):
            if confirmed and all(low[i] < low[i + k] for k in range(1, strength + 1)):
                lows.append({"index": i, "price": float(low[i]), "confirmed": True})
            elif not confirmed:
                lows.append({"index": i, "price": float(low[i]), "confirmed": False})
    return {"high": highs, "low": lows}


def _classify(direction: str, zone_low: float, zone_high: float, later_lows: Any, later_highs: Any, later_closes: Any) -> tuple:
    """Return ``(status, breaker, filled_ratio)`` for one block zone."""
    if later_lows.size == 0:
        return "unmitigated", False, 0.0
    if direction == "bullish":
        invalid = None
        for j in range(len(later_closes)):
            if later_closes[j] < zone_low:
                invalid = j
                break
        if invalid is not None:
            breaker = bool((later_closes[invalid + 1 :] > zone_high).any())
            return "invalidated", breaker, 1.0
        min_low = float(later_lows.min())
        if min_low <= zone_low:
            return "mitigated", False, 1.0
        if min_low < zone_high:
            return "mitigated", False, float(round((zone_high - min_low) / (zone_high - zone_low), 4))
        return "unmitigated", False, 0.0
    invalid = None
    for j in range(len(later_closes)):
        if later_closes[j] > zone_high:
            invalid = j
            break
    if invalid is not None:
        breaker = bool((later_closes[invalid + 1 :] < zone_low).any())
        return "invalidated", breaker, 1.0
    max_high = float(later_highs.max())
    if max_high >= zone_high:
        return "mitigated", False, 1.0
    if max_high > zone_low:
        return "mitigated", False, float(round((max_high - zone_low) / (zone_high - zone_low), 4))
    return "unmitigated", False, 0.0


def _make_block(direction: str, origin: int, leg_start: dict, leg_end: dict, high: Any, low: Any, close: Any, n: int, atr: float) -> dict:
    zone_low = float(low[origin])
    zone_high = float(high[origin])
    status, breaker, ratio = _classify(
        direction, zone_low, zone_high,
        low[origin + 1 : n], high[origin + 1 : n], close[origin + 1 : n],
    )
    impulse = leg_end["price"] - leg_start["price"]
    block = {
        "direction": direction,
        "high": round(zone_high, 8),
        "low": round(zone_low, 8),
        "index": origin,
        "swing_index": leg_start["index"],
        "status": status,
        "breaker": breaker,
        "filled_ratio": float(ratio),
        "leg_high": round(leg_end["price"], 8),
        "leg_low": round(leg_start["price"], 8),
        "leg_index": leg_end["index"],
        "impulse": round(impulse, 8),
        "atr": round(atr, 8),
    }
    if breaker:
        block["original_direction"] = direction
        block["direction"] = "breaker"
    return block


def detect_order_blocks(df: Any, cfg: Config) -> List[dict]:
    """Detect all order blocks from confirmed swing structure, most recent last."""
    _, high, low, close, _ = _bars(df)
    n = len(df)
    swings = find_swings(df, cfg.pivot_strength)
    confirmed = sorted(
        [{"index": s["index"], "price": s["price"], "side": "high"} for s in swings["high"] if s["confirmed"]]
        + [{"index": s["index"], "price": s["price"], "side": "low"} for s in swings["low"] if s["confirmed"]],
        key=lambda s: s["index"],
    )
    try:
        atr_arr = compute_atr(df, cfg.atr_period)
    except Exception:
        atr_arr = None

    def _atr_at(idx: int) -> float:
        if atr_arr is None:
            return max(float(close[n - 1]) * 0.01, 1e-9)
        try:
            value = float(atr_arr.iloc[idx])
            if value != value or value <= 0:
                raise ValueError
            return value
        except Exception:
            return max(float(close[idx]) * 0.01, 1e-9)

    blocks: List[dict] = []
    for a, b in zip(confirmed, confirmed[1:]):
        if b["index"] >= n:
            break
        atr = _atr_at(b["index"])
        if a["side"] == "low" and b["side"] == "high" and b["price"] > a["price"]:
            impulse = b["price"] - a["price"]
            if impulse < cfg.impulse_min_atr * atr:
                continue
            blocks.append(_make_block("bullish", a["index"], a, b, high, low, close, n, atr))
        elif a["side"] == "high" and b["side"] == "low" and b["price"] < a["price"]:
            impulse = a["price"] - b["price"]
            if impulse < cfg.impulse_min_atr * atr:
                continue
            blocks.append(_make_block("bearish", a["index"], a, b, high, low, close, n, atr))
    return blocks


def quality_score(block: dict, last: int, lookback: int) -> float:
    """Deterministic 0.0-1.0 quality: validity, precision, recency, impulse."""
    score = 0.0
    if block["breaker"]:
        score += 0.30
    elif block["status"] == "unmitigated":
        score += 0.40
    elif block["status"] == "mitigated":
        score += 0.20
    atr = block["atr"]
    size_ratio = (block["high"] - block["low"]) / atr
    score += 0.15 * max(0.0, 1.0 - abs(size_ratio - 1.0))
    recency = 1.0 - (last - block["index"]) / max(1, lookback)
    score += 0.25 * max(0.0, recency)
    score += 0.20 * min(1.0, block["impulse"] / (3.0 * atr))
    return round(min(1.0, max(0.0, score)), 4)


def quality_tier(score: float) -> str:
    if score >= 0.60:
        return "high"
    if score >= 0.35:
        return "medium"
    return "low"


def neutral_result(symbol: str, timeframe: str, reason: str) -> dict:
    """Fail-open result when the input is insufficient or invalid."""
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "order_blocks": [],
        "count": 0,
        "reason": reason,
    }


class OrderBlockAnalyzer:
    """Deterministic order block analyzer.

    ``analyze(symbol, df)`` returns ``{"symbol", "timeframe", "order_blocks",
    "count", "reason"}``; blocks are ranked best-first. It never raises:
    invalid input yields ``neutral_result``.
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = (config or {}).get("order_block") or (config or {}).get("order_blocks") or {}
        self.cfg = Config(
            pivot_strength=int(cfg.get("pivot_strength", 2)),
            impulse_min_atr=float(cfg.get("impulse_min_atr", 1.0)),
            atr_period=int(cfg.get("atr_period", 14)),
            max_lookback=int(cfg.get("max_lookback", 60)),
            max_blocks=int(cfg.get("max_blocks", 100)),
            min_bars=int(cfg.get("min_bars", 16)),
            timeframe=str(cfg.get("timeframe", "default")),
        )

    def analyze(self, symbol: str, df: Any, timeframe: Optional[str] = None) -> dict:
        tf = timeframe or self.cfg.timeframe
        try:
            if df is None or len(df) < self.cfg.min_bars:
                return neutral_result(symbol, tf, "insufficient_data")
            last = len(df) - 1
            blocks = detect_order_blocks(df, self.cfg)
            kept = [b for b in blocks if last - b["index"] <= self.cfg.max_lookback]
            if self.cfg.max_blocks > 0 and len(kept) > self.cfg.max_blocks:
                kept = kept[-self.cfg.max_blocks :]
            for b in kept:
                b["timeframe"] = tf
                b["quality"] = quality_score(b, last, self.cfg.max_lookback)
            ranked = sorted(kept, key=lambda b: (-b["quality"], -b["index"]))
            for rank, b in enumerate(ranked, 1):
                b["rank"] = rank
                b["quality_tier"] = quality_tier(b["quality"])
            return {
                "symbol": symbol,
                "timeframe": tf,
                "order_blocks": ranked,
                "count": len(ranked),
                "reason": None,
            }
        except Exception:
            return neutral_result(symbol, tf, "analysis_failed")

    def analyze_timeframes(self, symbol: str, timeframes: Dict[str, Any]) -> dict:
        """Run the same analysis over several timeframes: ``{tf: result}``."""
        return {tf: self.analyze(symbol, df, timeframe=tf) for tf, df in timeframes.items()}
