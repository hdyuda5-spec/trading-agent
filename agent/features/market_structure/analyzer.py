"""Deterministic Market Structure analyzer.

Pure-algorithm SMC-style structure analysis over a candle series. No
machine learning, no LLM, no subjective drawings — every output is a fixed
function of the input OHLCV DataFrame.

Definitions (all deterministic):

- Swing High / Swing Low
  Fractal pivots: bar ``i`` is a swing high when ``high[i]`` strictly
  exceeds the ``strength`` highs before and after it (mirror for lows). A
  pivot is *confirmed* only when the full right confirmation window exists
  (``i + strength < len(df)``); trailing pivots are reported as
  ``confirmed=False`` candidates and never used as structure. This is what
  makes the output non-repainting.

- Swing Trend
  Classified from the two most recent confirmed swing highs and lows within
  ``trend_window`` bars: higher highs with non-lower lows -> ``bullish``;
  lower lows with non-higher highs -> ``bearish``; otherwise the close slope
  over ``trend_window`` bars decides, else ``neutral``.

- BOS (Break of Structure)
  Structure state, in the direction of the swing trend (also valid in a
  neutral trend): bullish BOS when price has traded above the most recent
  confirmed swing high since it formed; bearish BOS below the most recent
  confirmed swing low. BOS resets once a new swing confirms and becomes the
  latest level.

- CHOCH (Change of Character)
  First counter-trend crack: in a bullish trend, price has traded below the
  most recent confirmed swing low (bearish CHOCH); in a bearish trend, above
  the most recent confirmed swing high (bullish CHOCH). Requires a trend.

- MSS (Market Structure Shift)
  Stronger form of CHOCH: the *close* (body) has traded beyond the
  counter-trend level, not just the wick. MSS implies CHOCH.

- Internal / External Structure
  The current dealing range is the most recent confirmed swing high and low
  inside ``range_window`` bars. Swing highs above the range high and swing
  lows below the range low are *external* structure; everything else inside
  the range is *internal*.
"""

from dataclasses import dataclass
from typing import Any, List, Optional

import numpy as np

from agent.core.utils import compute_atr


@dataclass
class Config:
    pivot_strength: int = 2
    structure_lookback: int = 60
    range_window: int = 30
    trend_window: int = 12
    atr_period: int = 14
    min_bars: int = 16
    volume_ratio_window: int = 20


def _bars(df: Any) -> tuple:
    return (
        df["open"].to_numpy(dtype=float),
        df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
        df["volume"].to_numpy(dtype=float),
    )


def find_swings(df: Any, strength: int = 2) -> dict:
    """Confirmed + candidate fractal swing highs/lows.

    Each pivot is ``{"index", "price", "confirmed"}``. Confirmed only when
    the full right window exists (``i + strength < len(df)``), so historical
    output never repaints as the series grows.
    """
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


def _swing_trend(df: Any, swings: dict, last: int, window: int) -> str:
    """Bullish / bearish / neutral from recent confirmed swing structure."""
    hi = [s for s in swings["high"] if s["confirmed"] and last - s["index"] <= window]
    lo = [s for s in swings["low"] if s["confirmed"] and last - s["index"] <= window]
    if len(hi) >= 2 and len(lo) >= 2:
        h_up = hi[-1]["price"] > hi[-2]["price"]
        h_dn = hi[-1]["price"] < hi[-2]["price"]
        l_up = lo[-1]["price"] > lo[-2]["price"]
        l_dn = lo[-1]["price"] < lo[-2]["price"]
        if h_up and not l_dn:
            return "bullish"
        if l_dn and not h_up:
            return "bearish"
    _, _, _, close, _ = _bars(df)
    if last >= window:
        diff = close[last] - close[last - window]
        if diff > 0:
            return "bullish"
        if diff < 0:
            return "bearish"
    return "neutral"


def _recent_levels(swings: dict, last: int, lookback: int) -> tuple:
    highs = [s for s in swings["high"] if s["confirmed"] and last - s["index"] <= lookback]
    lows = [s for s in swings["low"] if s["confirmed"] and last - s["index"] <= lookback]
    return highs, lows


def _lv(swing: dict, side: str) -> dict:
    return {"index": swing["index"], "price": round(swing["price"], 8), "side": side}


def _level_location(level: dict, rng: dict) -> str:
    if level["side"] == "high":
        return "external" if rng.get("high") is not None and level["price"] > rng["high"] else "internal"
    return "external" if rng.get("low") is not None and level["price"] < rng["low"] else "internal"


def detect_bos(highs: list, lows: list, trend: str, last: int, high: Any, low: Any) -> dict:
    """Break of Structure: in-trend break of the latest confirmed swing."""
    empty = {"bos": False, "direction": None, "level": None, "pierce": 0.0}
    if trend in ("bullish", "neutral") and highs:
        lv = highs[-1]
        window = high[lv["index"] + 1 : last + 1]
        if window.size and window.max() > lv["price"]:
            return {
                "bos": True,
                "direction": "bullish",
                "level": _lv(lv, "high"),
                "pierce": float(window.max() - lv["price"]),
            }
    if trend in ("bearish", "neutral") and lows:
        lv = lows[-1]
        window = low[lv["index"] + 1 : last + 1]
        if window.size and window.min() < lv["price"]:
            return {
                "bos": True,
                "direction": "bearish",
                "level": _lv(lv, "low"),
                "pierce": float(lv["price"] - window.min()),
            }
    return empty


def detect_choch(highs: list, lows: list, trend: str, last: int, high: Any, low: Any) -> dict:
    """Change of Character: counter-trend break of the latest swing (wick)."""
    empty = {"choch": False, "direction": None, "level": None, "pierce": 0.0}
    if trend == "bullish" and lows:
        lv = lows[-1]
        window = low[lv["index"] + 1 : last + 1]
        if window.size and window.min() < lv["price"]:
            return {
                "choch": True,
                "direction": "bearish",
                "level": _lv(lv, "low"),
                "pierce": float(lv["price"] - window.min()),
            }
    if trend == "bearish" and highs:
        lv = highs[-1]
        window = high[lv["index"] + 1 : last + 1]
        if window.size and window.max() > lv["price"]:
            return {
                "choch": True,
                "direction": "bullish",
                "level": _lv(lv, "high"),
                "pierce": float(window.max() - lv["price"]),
            }
    return empty


def detect_mss(highs: list, lows: list, trend: str, last: int, high: Any, low: Any, close: Any) -> dict:
    """Market Structure Shift: counter-trend break *closed* beyond the level."""
    empty = {"mss": False, "direction": None, "level": None, "pierce": 0.0}
    if trend == "bullish" and lows:
        lv = lows[-1]
        window = low[lv["index"] + 1 : last + 1]
        cwindow = close[lv["index"] + 1 : last + 1]
        if window.size and window.min() < lv["price"] and cwindow.min() < lv["price"]:
            return {
                "mss": True,
                "direction": "bearish",
                "level": _lv(lv, "low"),
                "pierce": float(lv["price"] - cwindow.min()),
            }
    if trend == "bearish" and highs:
        lv = highs[-1]
        window = high[lv["index"] + 1 : last + 1]
        cwindow = close[lv["index"] + 1 : last + 1]
        if window.size and window.max() > lv["price"] and cwindow.max() > lv["price"]:
            return {
                "mss": True,
                "direction": "bullish",
                "level": _lv(lv, "high"),
                "pierce": float(cwindow.max() - lv["price"]),
            }
    return empty


def dealing_range(swings: dict, last: int, window: int) -> dict:
    """Most recent confirmed swing high/low inside ``window`` bars."""
    highs = [s for s in swings["high"] if s["confirmed"] and s["index"] >= last - window]
    lows = [s for s in swings["low"] if s["confirmed"] and s["index"] >= last - window]
    rng: dict = {}
    if highs:
        rng["high"], rng["high_index"] = highs[-1]["price"], highs[-1]["index"]
    if lows:
        rng["low"], rng["low_index"] = lows[-1]["price"], lows[-1]["index"]
    return rng


def classify_structure(swings: dict, rng: dict, last: int, lookback: int) -> tuple:
    """Internal (inside dealing range) vs external (beyond it) structure."""
    internal: List[dict] = []
    external: List[dict] = []
    for s in swings["high"]:
        if not s["confirmed"] or last - s["index"] > lookback:
            continue
        location = "external" if rng.get("high") is not None and s["price"] > rng["high"] else "internal"
        entry = {"index": s["index"], "price": round(s["price"], 8), "side": "high", "location": location}
        (external if location == "external" else internal).append(entry)
    for s in swings["low"]:
        if not s["confirmed"] or last - s["index"] > lookback:
            continue
        location = "external" if rng.get("low") is not None and s["price"] < rng["low"] else "internal"
        entry = {"index": s["index"], "price": round(s["price"], 8), "side": "low", "location": location}
        (external if location == "external" else internal).append(entry)
    return internal, external


def structure_confidence(kind: str, level: dict, pierce: float, df: Any, atr: Optional[float], cfg: Config, last: int) -> float:
    """Deterministic 0.5-0.97 confidence for the strongest active event."""
    _, _, _, close, volume = _bars(df)
    c = close[last]
    atr = atr if atr and atr > 0 else max(c * 0.01, 1e-9)

    score = {"mss": 0.62, "choch": 0.56, "bos": 0.55}[kind]
    score += min(0.12, (pierce / atr) * 0.06)
    if kind == "mss":
        score += 0.10
    if level and level.get("location") == "external":
        score += 0.06
    window = max(1, min(cfg.volume_ratio_window, last))
    mean_vol = float(np.mean(volume[last - window : last])) or 1.0
    if volume[last] / mean_vol >= 1.2:
        score += 0.08
    return round(min(score, 0.97), 4)


def neutral_result(symbol: str, reason: str) -> dict:
    """Fail-open result when the input is insufficient or invalid."""
    return {
        "symbol": symbol,
        "trend": "neutral",
        "bos": False,
        "bos_direction": None,
        "bos_level": None,
        "choch": False,
        "choch_direction": None,
        "choch_level": None,
        "mss": False,
        "mss_direction": None,
        "mss_level": None,
        "swing_high": [],
        "swing_low": [],
        "internal_structure": [],
        "external_structure": [],
        "dealing_range": {},
        "confidence": 0.5,
        "reason": reason,
    }


class MarketStructureAnalyzer:
    """Deterministic market-structure analyzer.

    ``analyze(symbol, df)`` returns the full result dict (see module
    docstring). It never raises: invalid input yields ``neutral_result``.
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = (config or {}).get("market_structure") or (config or {}).get("structure_analyzer") or {}
        self.cfg = Config(
            pivot_strength=int(cfg.get("pivot_strength", 2)),
            structure_lookback=int(cfg.get("structure_lookback", 60)),
            range_window=int(cfg.get("range_window", 30)),
            trend_window=int(cfg.get("trend_window", 12)),
            atr_period=int(cfg.get("atr_period", 14)),
            min_bars=int(cfg.get("min_bars", 16)),
            volume_ratio_window=int(cfg.get("volume_ratio_window", 20)),
        )

    def analyze(self, symbol: str, df: Any) -> dict:
        try:
            if df is None or len(df) < max(self.cfg.min_bars, 2 * self.cfg.pivot_strength + 1):
                return neutral_result(symbol, "insufficient_data")
            return self._analyze(symbol, df)
        except Exception:
            return neutral_result(symbol, "analysis_failed")

    def _analyze(self, symbol: str, df: Any) -> dict:
        last = len(df) - 1
        _, high, low, close, _ = _bars(df)
        swings = find_swings(df, self.cfg.pivot_strength)
        trend = _swing_trend(df, swings, last, self.cfg.trend_window)
        highs, lows = _recent_levels(swings, last, self.cfg.structure_lookback)
        rng = dealing_range(swings, last, self.cfg.range_window)

        bos = detect_bos(highs, lows, trend, last, high, low)
        choch = detect_choch(highs, lows, trend, last, high, low)
        mss = detect_mss(highs, lows, trend, last, high, low, close)
        internal, external = classify_structure(swings, rng, last, self.cfg.structure_lookback)

        for evt in (bos, choch, mss):
            if evt["level"] is not None:
                evt["level"]["location"] = _level_location(evt["level"], rng)

        if mss["mss"]:
            kind, level, pierce = "mss", mss["level"], mss["pierce"]
        elif choch["choch"]:
            kind, level, pierce = "choch", choch["level"], choch["pierce"]
        elif bos["bos"]:
            kind, level, pierce = "bos", bos["level"], bos["pierce"]
        else:
            kind = None

        confidence = 0.5
        if kind:
            try:
                atr = float(compute_atr(df, self.cfg.atr_period).iloc[last])
            except Exception:
                atr = None
            confidence = structure_confidence(kind, level, pierce, df, atr, self.cfg, last)

        return {
            "symbol": symbol,
            "trend": trend,
            "bos": bos["bos"],
            "bos_direction": bos["direction"],
            "bos_level": bos["level"],
            "choch": choch["choch"],
            "choch_direction": choch["direction"],
            "choch_level": choch["level"],
            "mss": mss["mss"],
            "mss_direction": mss["direction"],
            "mss_level": mss["level"],
            "swing_high": swings["high"],
            "swing_low": swings["low"],
            "internal_structure": internal,
            "external_structure": external,
            "dealing_range": rng,
            "confidence": confidence,
            "reason": None,
        }
