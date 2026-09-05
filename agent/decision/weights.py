"""Single source of truth for decision weights and thresholds (Phase 5/22).

Every numeric tuning value for the decision layer lives under
``config["decision"]``; nothing is hardcoded across modules.
"""

from typing import Dict

DEFAULT_WEIGHTS: Dict[str, float] = {
    "technical": 2,
    "trend": 2,
    "momentum": 1,
    "liquidity": 3,
    "order_flow": 2,
    "whale": 1,
    "pattern": 1,
    "ai": 2,
    "news": 1,
}

DEFAULT_THRESHOLDS = {"long": 0.0, "short": 0.0}
DEFAULT_MIN_MARGIN = 0.05


def load_weights(config=None) -> Dict[str, float]:
    merged = dict(DEFAULT_WEIGHTS)
    cfg = (config or {}).get("decision", {}) or {}
    overrides = cfg.get("weights", {}) or {}
    # Backward compat: the strategies engine historically used `ai_advisory`.
    if "ai_advisory" in overrides and "ai" not in overrides:
        overrides = dict(overrides)
        overrides["ai"] = overrides["ai_advisory"]
    for k, v in overrides.items():
        try:
            merged[k] = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"decision.weights.{k} harus angka, dapat {v!r}")
    for k, v in merged.items():
        if v < 0:
            raise ValueError(f"decision.weights.{k} tidak boleh negatif ({v})")
    return merged


def load_thresholds(config=None) -> Dict[str, float]:
    merged = dict(DEFAULT_THRESHOLDS)
    cfg = (config or {}).get("decision", {}) or {}
    th = cfg.get("thresholds", {}) or {}
    for k, v in th.items():
        try:
            merged[k] = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"decision.thresholds.{k} harus angka, dapat {v!r}")
    return merged


def load_min_margin(config=None) -> float:
    cfg = (config or {}).get("decision", {}) or {}
    try:
        return float(cfg.get("min_margin", DEFAULT_MIN_MARGIN))
    except (TypeError, ValueError):
        raise ValueError("decision.min_margin harus angka")


def total_weight(weights: Dict[str, float]) -> float:
    return sum(float(v) for v in weights.values())