"""Deterministic Order Block analyzer tests.

Covers the required schema (``order_blocks``), bullish/bearish order blocks,
mitigated/invalid status, breaker blocks, quality ranking, the no-candle-color
guarantee, determinism, fail-open behavior, and the Feature Engine wrapper.
"""

import numpy as np
import pytest

from agent.features import (
    FEATURE_REGISTRY,
    FeatureEngine,
    build_feature_engine,
    compute_df_features,
)
from agent.features.order_block import OrderBlockAnalyzer, OrderBlockFeature
from agent.features.order_block.analyzer import (
    neutral_result,
    quality_score,
    quality_tier,
)

from tests.conftest import make_df

PREFIX = [10, 11, 12, 13, 13.5, 13, 12.5, 12, 11]
UNMIT = PREFIX + [12.5, 13.5, 14.5, 15, 14.5, 14, 13.5, 13.5, 14, 14.5]
MITIGATED = PREFIX + [12.5, 13.5, 14.5, 15, 14.5, 14, 13.5, 11.6, 13, 13.5, 14]
INVALIDATED = PREFIX + [12.5, 13.5, 14.5, 15, 14.5, 14, 13.5, 12.2, 10.2, 9.8, 10.5]
BREAKER = PREFIX + [12.5, 13.5, 14.5, 15, 14.5, 14, 13.5, 12.2, 10.2, 11.2, 12, 12.5, 13]
BEARISH_UNMIT = [15, 14, 13, 12, 11.5, 12, 12.5, 13, 14, 12.5, 11.5, 10.5, 10, 10.5, 11, 11.5, 11.5, 11, 10.5]


def df_from(close):
    return make_df(close, high_offset=0.5, low_offset=0.5)


def analyzer(config=None):
    return OrderBlockAnalyzer(config or {})


def block_at(result, index):
    matches = [b for b in result["order_blocks"] if b["index"] == index]
    assert matches, f"no block at index {index}: {result['order_blocks']}"
    return matches[0]


# ---------------------------------------------------------------------- schema


def test_required_schema_present():
    a = analyzer().analyze("T", df_from(UNMIT))
    assert "order_blocks" in a
    assert a["order_blocks"]
    for b in a["order_blocks"]:
        assert b["direction"] in ("bullish", "bearish", "breaker")
        assert isinstance(b["high"], float)
        assert isinstance(b["low"], float)
        assert b["high"] >= b["low"]
        assert b["status"] in ("unmitigated", "mitigated", "invalidated")
        assert isinstance(b["rank"], int)


def test_deterministic_across_instances():
    df = df_from(MITIGATED)
    assert analyzer().analyze("T", df) == analyzer().analyze("T", df)


def test_all_floats_are_python_float():
    for b in analyzer().analyze("T", df_from(UNMIT))["order_blocks"]:
        assert type(b["high"]) is float
        assert type(b["low"]) is float
        assert type(b["filled_ratio"]) is float
        assert type(b["quality"]) is float


# ------------------------------------------------------------- direction types


def test_bullish_order_block_detected():
    a = analyzer().analyze("T", df_from(UNMIT))
    b = block_at(a, 8)
    assert b["direction"] == "bullish"
    assert b["low"] == 10.5
    assert b["high"] == 11.5
    assert b["status"] == "unmitigated"
    assert b["breaker"] is False
    assert b["leg_low"] == 10.5


def test_bearish_order_block_detected():
    a = analyzer().analyze("T", df_from(BEARISH_UNMIT))
    b = block_at(a, 8)
    assert b["direction"] == "bearish"
    assert b["low"] == 13.5
    assert b["high"] == 14.5
    assert b["status"] == "unmitigated"
    assert b["breaker"] is False


# ---------------------------------------------------------------------- status


def test_mitigated_block_partial():
    a = analyzer().analyze("T", df_from(MITIGATED))
    b = block_at(a, 8)
    assert b["status"] == "mitigated"
    assert b["breaker"] is False
    assert b["filled_ratio"] == 0.4


def test_invalid_block():
    a = analyzer().analyze("T", df_from(INVALIDATED))
    b = block_at(a, 8)
    assert b["status"] == "invalidated"
    assert b["breaker"] is False
    assert b["filled_ratio"] == 1.0


def test_breaker_block():
    a = analyzer().analyze("T", df_from(BREAKER))
    b = block_at(a, 8)
    assert b["direction"] == "breaker"
    assert b["original_direction"] == "bullish"
    assert b["breaker"] is True
    assert b["status"] == "invalidated"


# ------------------------------------------------------ no candle-color heuristic


def test_detection_independent_of_candle_color():
    """Flipping the origin candle to red must not change the block."""
    green = df_from(UNMIT)
    red = df_from(UNMIT)
    origin = 8
    red.loc[red.index[origin], "open"] = float(red["close"].iloc[origin]) + 0.3
    assert red["open"].iloc[origin] > red["close"].iloc[origin]
    g = analyzer().analyze("T", green)
    r = analyzer().analyze("T", red)
    assert g == r


# --------------------------------------------------------------------- ranking


def test_blocks_ranked_by_quality():
    a = analyzer().analyze("T", df_from(UNMIT))
    blocks = a["order_blocks"]
    assert [b["rank"] for b in blocks] == list(range(1, len(blocks) + 1))
    qualities = [b["quality"] for b in blocks]
    assert qualities == sorted(qualities, reverse=True)
    assert blocks[0]["status"] == "unmitigated"
    assert blocks[0]["quality"] > blocks[1]["quality"]


def test_quality_tiers():
    assert quality_tier(0.9335) == "high"
    assert quality_tier(0.5) == "medium"
    assert quality_tier(0.1) == "low"
    for b in analyzer().analyze("T", df_from(UNMIT))["order_blocks"]:
        assert b["quality_tier"] in ("high", "medium", "low")


def test_quality_score_range():
    df = df_from(UNMIT)
    last = len(df) - 1
    for b in analyzer().analyze("T", df)["order_blocks"]:
        assert 0.0 <= quality_score(b, last, 60) <= 1.0


# -------------------------------------------------------------------- config


def test_impulse_min_atr_filters_small_legs():
    df = df_from(UNMIT)
    assert analyzer().analyze("T", df)["order_blocks"]
    strict = analyzer({"order_block": {"impulse_min_atr": 100.0}}).analyze("T", df)
    assert strict["order_blocks"] == []


# ---------------------------------------------------------------- fail-open


def test_insufficient_data_neutral():
    a = analyzer().analyze("T", make_df([100.0] * 5, high_offset=0.5, low_offset=0.5))
    assert a["order_blocks"] == []
    assert a["count"] == 0
    assert a["reason"] == "insufficient_data"


def test_neutral_result_never_raises():
    r = neutral_result("T", "15m", "boom")
    assert r["order_blocks"] == []
    assert r["count"] == 0


# ---------------------------------------------------------------- multi-timeframe


def test_multi_timeframe_analysis():
    a = analyzer()
    result = a.analyze_timeframes(
        "T",
        {"5m": df_from(UNMIT), "1h": df_from(BEARISH_UNMIT)},
    )
    assert set(result) == {"5m", "1h"}
    assert block_at(result["5m"], 8)["direction"] == "bullish"
    assert block_at(result["1h"], 8)["direction"] == "bearish"
    for tf, res in result.items():
        for b in res["order_blocks"]:
            assert b["timeframe"] == tf
            assert res["timeframe"] == tf


# ------------------------------------------------------------- feature wrapper


def test_feature_wrapper_signal_bullish():
    f = OrderBlockFeature(None, {})
    fr = f.compute("T", df_from(UNMIT))
    assert fr.name == "order_blocks"
    assert fr.signal == "bullish"
    assert 0.5 <= fr.confidence <= 1.0
    assert fr.metadata["best_actionable"]["direction"] == "bullish"


def test_feature_wrapper_breaker_flips_signal():
    f = OrderBlockFeature(None, {})
    fr = f.compute("T", df_from(BREAKER))
    assert fr.signal == "bearish"


def test_feature_wrapper_df_only_and_opt_in():
    assert OrderBlockFeature.df_only is True
    assert OrderBlockFeature.opt_in is True
    fr = OrderBlockFeature(None, {}).compute("T", make_df([100.0] * 5))
    assert fr.signal == "neutral"
    assert fr.is_neutral


def test_feature_is_registered_opt_in():
    names = [f.name for f in FEATURE_REGISTRY]
    assert "order_blocks" in names
    engine = build_feature_engine(market_data=None, config={}, enabled=["order_blocks"])
    assert engine.has_feature("order_blocks")
    default = FeatureEngine(market_data=None, config={})
    assert not default.has_feature("order_blocks")


def test_feature_included_in_df_features_when_enabled():
    config = {"features": {"enabled": ["trend", "order_blocks"]}}
    fs = compute_df_features("T", df_from(UNMIT), config)
    assert "order_blocks" in fs
    assert fs["order_blocks"].metadata["order_blocks"]
