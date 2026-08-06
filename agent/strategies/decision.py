"""Feature-driven decision pipeline.

Implements the canonical decision flow::

    Trend -> Liquidity -> Structure -> Order Block -> FVG -> Risk Engine -> Decision

Nothing in this module (and no feature, and no strategy) places orders — the
pipeline only *votes* (weighted confidence per feature), *scores*, and — via
the Risk Engine — attaches stop-loss/take-profit before a Decision is emitted.

Every indicator value is read from Feature Engine outputs; strategies never
compute indicators from raw OHLCV. The Decision shape is::

    {
        "action": "BUY",          # BUY | SELL
        "confidence": 0.88,       # 0.0 - 1.0
        "reason": [...],          # ordered per-feature notes
        "risk": {"sl": ..., "tp": ..., "atr": ..., "entry": ..., "rr": ...},
    }

Compatibility fields ``side`` (LONG/SHORT), ``strategy``, ``symbol``,
``price`` and ``metadata`` are added so the bot's execution layer keeps
working unchanged.

Advisory input: the LLM strategy never emits a BUY/SELL verdict — it submits a
structured *advisory* (``{"trend": "bullish"|"bearish"|"neutral", "confidence":
0..1}``) which this engine folds in as a weighted ``ai_advisory`` vote. The
Decision Engine remains the sole authority on trade direction.
"""

from typing import Any, Dict, List, Optional

from agent.core.utils import is_valid_atr
from agent.features import (
    FEATURE_REGISTRY,
    FeatureSet,
    build_feature_engine,
)

# The canonical flow, in order. Each feature contributes a weighted vote.
CORE_FLOW = ("trend", "smart_money_liquidity", "structure", "order_blocks", "fvg")

DEFAULT_WEIGHTS = {
    "trend": 0.30,
    "smart_money_liquidity": 0.15,
    "structure": 0.15,
    "order_blocks": 0.25,
    "fvg": 0.15,
}

# Fallback weight for the LLM advisory vote (``DecisionEngine.decide`` reads
# ``weights["ai_advisory"]`` first; config may override). Not part of
# ``DEFAULT_WEIGHTS`` so the pure feature-engine total stays 1.0.
DEFAULT_ADVISORY_WEIGHT = 0.20

_ACTION_FROM_SIDE = {"LONG": "BUY", "SHORT": "SELL"}


def build_decision(
    strategy: str,
    symbol: str,
    side: str,
    confidence: float,
    reasons: List[str],
    risk: dict,
    price: float,
    metadata: Optional[dict] = None,
) -> dict:
    """Assemble the canonical Decision dict (with bot-compatible extras)."""
    confidence = round(min(1.0, max(0.0, float(confidence))), 4)
    return {
        "strategy": strategy,
        "symbol": symbol,
        "action": _ACTION_FROM_SIDE.get(side, side),
        "side": side,
        "confidence": confidence,
        "price": round(float(price), 8),
        "reason": [str(r) for r in reasons],
        "risk": dict(risk or {}),
        "metadata": dict(metadata or {}),
    }


class RiskEngine:
    """ATR-based SL/TP attached to a decision. Fail-open: no valid ATR -> no risk.

    Mirrors ``agent.core.risk.RiskEngine``'s ATR stop/take multipliers
    (``risk.atr_stop_mult`` / ``risk.atr_tp_mult``) so the decision carries the
    same levels the execution layer would have used.
    """

    def __init__(self, config: Optional[dict] = None):
        self.cfg = (config or {}).get("risk", {}) or {}
        self.atr_stop_mult = float(self.cfg.get("atr_stop_mult", 1.5))
        self.atr_tp_mult = float(self.cfg.get("atr_tp_mult", 2.5))

    def compute(self, entry: float, side: str, atr) -> Optional[dict]:
        if not is_valid_atr(atr):
            return None
        if side == "LONG":
            sl = entry - self.atr_stop_mult * float(atr)
            tp = entry + self.atr_tp_mult * float(atr)
        else:
            sl = entry + self.atr_stop_mult * float(atr)
            tp = entry - self.atr_tp_mult * float(atr)
        if sl <= 0 or (tp - entry) * (entry - sl) <= 0:
            return None
        dist = abs(entry - sl)
        rr = round(abs(tp - entry) / dist, 4) if dist > 0 else 0.0
        return {
            "entry": round(float(entry), 8),
            "sl": round(float(sl), 8),
            "tp": round(float(tp), 8),
            "atr": round(float(atr), 8),
            "rr": rr,
        }


class DecisionEngine:
    """Runs the canonical flow and returns a weighted vote verdict.

    ``decide(symbol, df, features=None)`` returns a dict with
    ``votes`` (ordered per-feature votes with notes), ``scores``,
    ``margin`` (in [-1, 1]), ``vote_side`` (LONG/SHORT/None), ``reasons``,
    ``price``, ``atr`` and the resolved ``features``. Strategies turn this
    verdict into a Decision through ``RiskEngine`` + ``build_decision``.
    """

    def __init__(self, config: Optional[dict] = None, weights: Optional[dict] = None, feature_engine=None):
        self.config = config or {}
        merged = dict(DEFAULT_WEIGHTS)
        merged.update(weights or {})
        if "ai_advisory" not in merged:
            merged["ai_advisory"] = DEFAULT_ADVISORY_WEIGHT
        self.weights = {k: max(0.0, float(v)) for k, v in merged.items()}
        self.total_weight = sum(self.weights.get(k, 0.0) for k in CORE_FLOW)
        self.feature_engine = feature_engine
        self._engine = None
        self.min_margin = float((self.config.get("decision", {}) or {}).get("min_margin", 0.15))

    def _full_engine(self):
        if self._engine is None:
            enabled = [f.name for f in FEATURE_REGISTRY if getattr(f, "df_only", False)]
            self._engine = build_feature_engine(market_data=None, config=self.config, enabled=enabled)
        return self._engine

    def resolve_features(self, symbol: str, df: Any, features=None) -> FeatureSet:
        """Full df-only feature stack; merge any caller-supplied features."""
        full = self._full_engine().compute(symbol, df, include_market=False)
        if features is None:
            return full
        merged = {name: features.get(name) if features.get(name) is not None else full[name]
                  for name in set(list(full.names()) + list(features.names()))}
        return FeatureSet(symbol=symbol, features=merged, computed_at=full.computed_at)

    @staticmethod
    def _direction(signal: str) -> Optional[str]:
        if signal == "bullish":
            return "LONG"
        if signal == "bearish":
            return "SHORT"
        return None

    def _vote(self, name: str, fr) -> dict:
        side = None
        confidence = float(fr.confidence) if fr is not None else 0.0
        if fr is None:
            return {"feature": name, "side": None, "confidence": 0.0,
                    "note": f"{name}: tidak tersedia"}
        signal = fr.signal
        note = f"{name}: {signal}"
        if name == "trend":
            side = self._direction(signal)
        elif name == "smart_money_liquidity":
            side = self._direction(signal)
            note = f"liquidity: {signal} (sweep)" if signal != "neutral" else "liquidity: neutral"
        elif name == "structure":
            side = self._direction(signal)
            meta = fr.metadata
            if meta.get("near_support"):
                note = "structure: dekat support"
            elif meta.get("near_resistance"):
                note = "structure: dekat resistance"
        elif name == "order_blocks":
            meta = fr.metadata
            best = None
            for b in meta.get("order_blocks", []):
                if b.get("status") == "unmitigated" or b.get("breaker"):
                    best = b
                    break
            if best is not None:
                direction = best.get("direction")
                original = best.get("original_direction")
                if direction == "breaker":
                    side = "SHORT" if original == "bullish" else "LONG"
                else:
                    side = "LONG" if direction == "bullish" else "SHORT"
                note = f"order_block: {best.get('status')} {direction} idx {best.get('index')}"
            else:
                note = "order_block: tidak ada block actionable"
        elif name == "fvg":
            latest = fr.metadata.get("latest_unmitigated")
            if latest is not None:
                side = "LONG" if latest["direction"] == "bullish" else "SHORT"
                note = f"fvg: gap {latest['direction']} idx {latest.get('index')}"
            else:
                note = "fvg: tidak ada gap unmitigated"
        return {"feature": name, "side": side, "confidence": confidence, "note": note}

    def decide(self, symbol: str, df: Any, features=None, advisory: Optional[dict] = None) -> Optional[dict]:
        if df is None or len(df) < 5:
            return None
        fs = self.resolve_features(symbol, df, features)
        price = float(df["close"].iloc[-1])
        votes = [self._vote(name, fs.get(name)) for name in CORE_FLOW]

        advisory_weight = 0.0
        advisory_used = None
        if isinstance(advisory, dict):
            trend = str(advisory.get("trend") or "").strip().lower()
            try:
                conf = max(0.0, min(1.0, float(advisory.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                conf = 0.0
            side = {"bullish": "LONG", "bearish": "SHORT"}.get(trend)
            aw = max(0.0, float(self.weights.get("ai_advisory", DEFAULT_ADVISORY_WEIGHT)))
            if side and aw > 0:
                votes.append(
                    {
                        "feature": "ai_advisory",
                        "side": side,
                        "confidence": conf,
                        "note": f"ai_advisory: {trend} (LLM confidence {conf:.0%})",
                    }
                )
                advisory_weight = aw
                advisory_used = {"trend": trend, "confidence": conf, "weight": aw}

        total_weight = self.total_weight + advisory_weight
        scores = {"LONG": 0.0, "SHORT": 0.0}
        for v in votes:
            if v["side"] and total_weight > 0:
                scores[v["side"]] += self.weights.get(v["feature"], 0.0) * v["confidence"]
        margin = (scores["LONG"] - scores["SHORT"]) / total_weight if total_weight > 0 else 0.0
        if margin > 0:
            vote_side = "LONG"
        elif margin < 0:
            vote_side = "SHORT"
        else:
            vote_side = None

        confidence = min(0.95, 0.5 + 0.5 * abs(margin))
        reasons = [v["note"] for v in votes]

        vola = fs.get("volatility")
        atr = None
        if vola is not None:
            atr = vola.metadata.get("atr")

        return {
            "symbol": symbol,
            "price": price,
            "atr": atr,
            "features": fs,
            "votes": votes,
            "scores": scores,
            "margin": round(margin, 4),
            "vote_side": vote_side,
            "confidence": round(confidence, 4),
            "reasons": reasons,
            "advisory": advisory_used,
        }
