"""Deterministic Smart Money liquidity analyzer.

This module answers *structure* questions about a candle series using only
fixed rules on OHLCV — no subjective drawings. Every result is a pure
function of the input DataFrame, so the same input always produces the same
output (deterministic) and historical bars never change once more data
arrives (non-repainting: pivots are only *confirmed* once their full
confirmation window has closed, and sweeps are only evaluated on the latest
bar against confirmed levels).

Concepts and their deterministic definitions:

- Swing High / Swing Low
  A fractal pivot: bar ``i`` is a swing high when ``high[i]`` is strictly
  greater than the ``strength`` highs before and after it; a swing low is
  the symmetric condition on ``low``. A pivot is *confirmed* only when
  ``strength`` bars exist after it; pivots in the trailing ``strength`` bars
  are reported as ``confirmed=False`` candidates and never used for levels.

- Equal High / Equal Low
  Confirmed swing levels are clustered by price proximity. A cluster whose
  anchor is within ``equal_tolerance_pct`` of a member is grouped; clusters
  with >= 2 members are Equal Highs (clusters of swing highs) or Equal Lows
  (clusters of swing lows). The cluster price is the mean of its members.

- Buy Side Liquidity / Sell Side Liquidity
  Buy-side liquidity pools form at swing highs and equal highs (where resting
  buy orders and short-stops sit); sell-side pools form at swing lows and
  equal lows (where resting sell orders and long-stops sit). Each level is
  reported with its price, source, touch count and classification.

- Liquidity Sweep / Stop Hunt
  A sweep occurs when the latest bar's wick trades beyond a recent confirmed
  liquidity level and the close returns *through* it (body rejection):
  piercing below a sell-side level and closing above it is a bullish sweep;
  piercing above a buy-side level and closing below it is a bearish sweep.
  A sweep on a level with >= 2 touches (an equal high/low = a demonstrable
  stop cluster) is additionally flagged as a *stop hunt*.

- Internal Liquidity / External Liquidity
  The current dealing range is defined by the most recent confirmed swing
  high and swing low inside ``range_window`` bars. Buy-side levels above the
  range high and sell-side levels below the range low are *external*
  liquidity; all levels inside the range are *internal*.
"""

from dataclasses import dataclass
from typing import Any, List, Optional

import numpy as np

from agent.core.utils import compute_atr


@dataclass
class Config:
    pivot_strength: int = 2
    equal_tolerance_pct: float = 0.15
    range_window: int = 30
    liquidity_lookback: int = 60
    sweep_lookback: int = 6
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
    """Return confirmed + candidate swing highs/lows.

    Each pivot is ``{"index", "price", "confirmed"}``. A pivot at index ``i``
    is confirmed only when ``i + strength < len(df)``, i.e. the full right
    confirmation window exists — the guarantee that historical output never
    repaints as the series grows.
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


def cluster_levels(swings: List[dict], tolerance_pct: float) -> List[dict]:
    """Group swing levels by price proximity.

    Deterministic anchor-based clustering: members are visited in ascending
    price order and join the first cluster whose anchor is within
    ``tolerance_pct`` of them. Returns clusters of
    ``{"price": mean, "touches": n, "indices": [...]}``.
    """
    ordered = sorted(swings, key=lambda s: s["price"])
    clusters: List[dict] = []
    for s in ordered:
        for c in clusters:
            if abs(s["price"] - c["anchor"]) / c["anchor"] * 100.0 <= tolerance_pct:
                c["members"].append(s)
                break
        else:
            clusters.append({"anchor": s["price"], "members": [s]})
    return [
        {
            "price": float(np.mean([m["price"] for m in c["members"]])),
            "touches": len(c["members"]),
            "indices": sorted(m["index"] for m in c["members"]),
        }
        for c in clusters
    ]


def dealing_range(df: Any, swings: dict, window: int = 30) -> dict:
    """Most recent confirmed swing high/low inside the last ``window`` bars."""
    last = len(df) - 1
    highs = [s for s in swings["high"] if s["confirmed"] and s["index"] >= last - window]
    lows = [s for s in swings["low"] if s["confirmed"] and s["index"] >= last - window]
    rng: dict = {}
    if highs:
        h = highs[-1]
        rng["high"] = h["price"]
        rng["high_index"] = h["index"]
    if lows:
        l = lows[-1]
        rng["low"] = l["price"]
        rng["low_index"] = l["index"]
    return rng


def _level_entry(cluster: dict, side: str, source: str, rng: dict) -> dict:
    price = cluster["price"]
    if side == "buy":
        location = "external" if "high" in rng and price > rng["high"] else "internal"
    else:
        location = "external" if "low" in rng and price < rng["low"] else "internal"
    return {
        "price": round(price, 8),
        "side": side,
        "source": source,
        "touches": cluster["touches"],
        "index": max(cluster["indices"]),
        "location": location,
    }


def build_levels(df: Any, swings: dict, cfg: Config) -> dict:
    """Buy-side + sell-side liquidity levels from confirmed structure."""
    last = len(df) - 1
    confirmed_highs = [s for s in swings["high"] if s["confirmed"]]
    confirmed_lows = [s for s in swings["low"] if s["confirmed"]]
    rng = dealing_range(df, swings, cfg.range_window)
    buy = []
    sell = []
    for cluster in cluster_levels(confirmed_highs, cfg.equal_tolerance_pct):
        if max(cluster["indices"]) < last - cfg.liquidity_lookback:
            continue
        source = "equal_high" if cluster["touches"] >= 2 else "swing_high"
        buy.append(_level_entry(cluster, "buy", source, rng))
    for cluster in cluster_levels(confirmed_lows, cfg.equal_tolerance_pct):
        if max(cluster["indices"]) < last - cfg.liquidity_lookback:
            continue
        source = "equal_low" if cluster["touches"] >= 2 else "swing_low"
        sell.append(_level_entry(cluster, "sell", source, rng))
    return {"buy": buy, "sell": sell, "range": rng}


def detect_sweep(
    df: Any,
    levels: dict,
    cfg: Config,
    last: int,
) -> Optional[dict]:
    """Evaluate the latest bar for a liquidity sweep (deterministic).

    Bullish sweep: the low pierces below a recent confirmed sell-side level
    while the close is back above that level, and the low is below the body.
    Bearish sweep: the high pierces above a recent confirmed buy-side level
    while the close is back below it, and the high is above the body.

    Returns ``None`` when no sweep is detected.
    """
    _, high, low, close, _ = _bars(df)
    o, h, l, c = df["open"].iloc[last], high[last], low[last], close[last]
    body_low = min(o, c)
    body_high = max(o, c)

    bull_candidates = [
        lv
        for lv in levels["sell"]
        if last - lv["index"] <= cfg.sweep_lookback and lv["price"] > l and lv["price"] < c and l < body_low
    ]
    bear_candidates = [
        lv
        for lv in levels["buy"]
        if last - lv["index"] <= cfg.sweep_lookback and lv["price"] < h and lv["price"] > c and h > body_high
    ]

    if bull_candidates:
        level = min(bull_candidates, key=lambda lv: (lv["price"] - l, -lv["index"]))
        return {
            "direction": "bullish",
            "level": level,
            "bar_index": last,
            "pierce": float(level["price"] - l),
            "stop_hunt": level["touches"] >= 2,
        }
    if bear_candidates:
        level = min(bear_candidates, key=lambda lv: (h - lv["price"], -lv["index"]))
        return {
            "direction": "bearish",
            "level": level,
            "bar_index": last,
            "pierce": float(h - level["price"]),
            "stop_hunt": level["touches"] >= 2,
        }
    return None


def sweep_confidence(sweep: dict, df: Any, atr: Optional[float], cfg: Config, last: int) -> float:
    """Deterministic 0.5–0.97 confidence for a detected sweep."""
    _, _, _, close, volume = _bars(df)
    c = close[last]
    level = sweep["level"]
    atr = atr if atr and atr > 0 else max(c * 0.01, 1e-9)

    score = 0.55
    score += min(0.12, (sweep["pierce"] / atr) * 0.06)
    if sweep["stop_hunt"]:
        score += 0.10
    if level["location"] == "external":
        score += 0.06
    body_reject = (c - level["price"]) if sweep["direction"] == "bullish" else (level["price"] - c)
    if body_reject >= 0.5 * atr:
        score += 0.06
    window = max(1, min(cfg.volume_ratio_window, last))
    mean_vol = float(np.mean(volume[last - window : last])) or 1.0
    vol_ratio = float(volume[last]) / mean_vol
    if vol_ratio >= 1.2:
        score += 0.08
    return round(min(score, 0.97), 4)


def neutral_result(symbol: str, reason: str) -> dict:
    """Fail-open result when the input is insufficient or invalid."""
    return {
        "symbol": symbol,
        "swing_highs": [],
        "swing_lows": [],
        "equal_highs": [],
        "equal_lows": [],
        "buy_side_liquidity": [],
        "sell_side_liquidity": [],
        "internal_liquidity": [],
        "external_liquidity": [],
        "dealing_range": {},
        "liquidity_sweep": False,
        "sweep_direction": None,
        "stop_hunt": False,
        "sweep": None,
        "confidence": 0.5,
        "reason": reason,
    }


class SmartMoneyLiquidityAnalyzer:
    """Deterministic structural liquidity analyzer.

    ``analyze(symbol, df)`` returns the full result dict (see module
    docstring). It never raises: invalid input yields ``neutral_result``.
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = (config or {}).get("smart_money_liquidity") or (config or {}).get("liquidity_analyzer") or {}
        self.cfg = Config(
            pivot_strength=int(cfg.get("pivot_strength", 2)),
            equal_tolerance_pct=float(cfg.get("equal_tolerance_pct", 0.15)),
            range_window=int(cfg.get("range_window", 30)),
            liquidity_lookback=int(cfg.get("liquidity_lookback", 60)),
            sweep_lookback=int(cfg.get("sweep_lookback", 6)),
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
        swings = find_swings(df, self.cfg.pivot_strength)
        levels = build_levels(df, swings, self.cfg)
        rng = levels["range"]
        sweep = detect_sweep(df, levels, self.cfg, last)
        confidence = 0.5
        if sweep is not None:
            try:
                atr = float(compute_atr(df, self.cfg.atr_period).iloc[last])
            except Exception:
                atr = None
            confidence = sweep_confidence(sweep, df, atr, self.cfg, last)

        equal_highs = [lv for lv in levels["buy"] if lv["source"] == "equal_high"]
        equal_lows = [lv for lv in levels["sell"] if lv["source"] == "equal_low"]
        internal = [
            lv for lv in levels["buy"] + levels["sell"] if lv["location"] == "internal"
        ]
        external = [
            lv for lv in levels["buy"] + levels["sell"] if lv["location"] == "external"
        ]

        return {
            "symbol": symbol,
            "swing_highs": swings["high"],
            "swing_lows": swings["low"],
            "equal_highs": equal_highs,
            "equal_lows": equal_lows,
            "buy_side_liquidity": levels["buy"],
            "sell_side_liquidity": levels["sell"],
            "internal_liquidity": internal,
            "external_liquidity": external,
            "dealing_range": rng,
            "liquidity_sweep": sweep is not None,
            "sweep_direction": sweep["direction"] if sweep else None,
            "stop_hunt": bool(sweep and sweep["stop_hunt"]),
            "sweep": sweep,
            "confidence": confidence,
            "reason": None,
        }
