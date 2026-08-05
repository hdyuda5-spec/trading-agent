"""Deterministic Market Structure analyzer tests.

Covers the required schema (trend/bos/choch/mss/swing_high/swing_low), swing
trend classification, BOS, CHOCH, MSS, internal/external structure,
determinism, the non-repainting guarantee, and the Feature Engine wrapper.
"""

import numpy as np
import pytest

from agent.features import (
    FEATURE_REGISTRY,
    FeatureEngine,
    build_feature_engine,
    compute_df_features,
)
from agent.features.market_structure import (
    MarketStructureAnalyzer,
    MarketStructureFeature,
)
from agent.features.market_structure.analyzer import (
    find_swings,
    neutral_result,
)

from tests.conftest import make_df

REQUIRED_KEYS = {
    "trend",
    "bos",
    "choch",
    "mss",
    "swing_high",
    "swing_low",
}

ZIGZAG = [10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10]
BULL_BOS = [10, 11, 12, 11.5, 11, 10.5, 11, 11.5, 12, 13, 12.5, 12, 11.5, 12, 12.5, 13, 14, 14.5]
BEAR_BOS = [14, 13, 12, 12.5, 13, 13.5, 13, 12.5, 12, 11, 11.5, 12, 12.5, 12, 11.5, 11, 10, 9.5]
CHOCK = [10, 11, 12, 13, 13.5, 13, 12.5, 11.5, 12.5, 13.5, 14.5, 14, 13.5, 13.6, 13.8, 14.2, 14.5, 14.2, 13.9, 13.2]
MSSK = [10, 11, 12, 13, 13.5, 13, 12.5, 11.5, 12.5, 13.5, 14.5, 14, 13.5, 13.6, 13.8, 14.2, 14.5, 14.2, 13.9, 12.8]
STRUCTURE = [12, 13, 13.5, 13, 12.5, 12, 11.5, 11, 10.5, 10, 9.5, 10, 10.5, 11, 11.5, 12, 12.5, 12.8, 12.6, 12.4, 12.6, 12.5, 12.7, 12.6]


def df_from(close, **kw):
    return make_df(close, high_offset=0.5, low_offset=0.5, **kw)


def analyzer(config=None):
    return MarketStructureAnalyzer(config or {})


# ---------------------------------------------------------------------- schema


def test_required_schema_present():
    a = analyzer().analyze("T", df_from(BULL_BOS))
    assert REQUIRED_KEYS <= set(a)
    assert isinstance(a["trend"], str)
    assert isinstance(a["bos"], bool)
    assert isinstance(a["choch"], bool)
    assert isinstance(a["mss"], bool)
    assert isinstance(a["swing_high"], list)
    assert isinstance(a["swing_low"], list)


def test_deterministic_across_instances():
    df = df_from(CHOCK)
    assert analyzer().analyze("T", df) == analyzer().analyze("T", df)


# ----------------------------------------------------------------------- swings


def test_swing_highs_and_lows_detected():
    sw = find_swings(df_from(ZIGZAG), strength=2)
    assert [(s["index"], s["price"]) for s in sw["high"] if s["confirmed"]] == [(2, 12.5), (9, 13.5)]
    assert [(s["index"], s["price"]) for s in sw["low"] if s["confirmed"]] == [(5, 8.5), (14, 7.5)]


def test_confirmed_pivots_need_right_window():
    n = len(ZIGZAG)
    sw = find_swings(df_from(ZIGZAG), strength=2)
    for pivot in sw["high"] + sw["low"]:
        if pivot["confirmed"]:
            assert pivot["index"] + 2 < n


def test_swing_lists_exposed_in_result():
    a = analyzer().analyze("T", df_from(ZIGZAG))
    assert [(s["index"], s["price"]) for s in a["swing_high"] if s["confirmed"]] == [(2, 12.5), (9, 13.5)]
    assert [(s["index"], s["price"]) for s in a["swing_low"] if s["confirmed"]] == [(5, 8.5), (14, 7.5)]


# ----------------------------------------------------------------------- trend


def test_swing_trend_bullish():
    assert analyzer().analyze("T", df_from(BULL_BOS))["trend"] == "bullish"


def test_swing_trend_bearish():
    assert analyzer().analyze("T", df_from(BEAR_BOS))["trend"] == "bearish"


def test_swing_trend_neutral_on_zigzag():
    assert analyzer().analyze("T", df_from(ZIGZAG))["trend"] == "neutral"


# ----------------------------------------------------------------------- BOS


def test_bullish_bos_detected():
    a = analyzer().analyze("T", df_from(BULL_BOS))
    assert a["bos"] is True
    assert a["bos_direction"] == "bullish"
    assert a["bos_level"]["side"] == "high"
    assert a["bos_level"]["index"] == 9
    assert a["bos_level"]["price"] == 13.5
    assert a["choch"] is False
    assert a["mss"] is False


def test_bearish_bos_detected():
    a = analyzer().analyze("T", df_from(BEAR_BOS))
    assert a["bos"] is True
    assert a["bos_direction"] == "bearish"
    assert a["bos_level"]["side"] == "low"
    assert a["bos_level"]["index"] == 9
    assert a["bos_level"]["price"] == 10.5
    assert a["choch"] is False


def test_no_bos_without_structure_break():
    a = analyzer().analyze("T", df_from(ZIGZAG))
    assert a["bos"] is False
    assert a["bos_direction"] is None
    assert a["bos_level"] is None


# ---------------------------------------------------------------------- CHOCH


def test_bearish_choch_detected():
    a = analyzer().analyze("T", df_from(CHOCK))
    assert a["choch"] is True
    assert a["choch_direction"] == "bearish"
    assert a["choch_level"]["side"] == "low"
    assert a["mss"] is False
    assert a["bos"] is False


def test_bearish_mss_detected():
    a = analyzer().analyze("T", df_from(MSSK))
    assert a["mss"] is True
    assert a["mss_direction"] == "bearish"
    assert a["choch"] is True


def test_bullish_choch_and_mss_detected():
    mirror_c = [round(25.0 - c, 4) for c in CHOCK]
    mirror_m = [round(25.0 - c, 4) for c in MSSK]
    c = analyzer().analyze("T", df_from(mirror_c))
    assert c["trend"] == "bearish"
    assert c["choch"] is True
    assert c["choch_direction"] == "bullish"
    m = analyzer().analyze("T", df_from(mirror_m))
    assert m["mss"] is True
    assert m["mss_direction"] == "bullish"


def test_no_choch_without_trend():
    a = analyzer().analyze("T", df_from(ZIGZAG))
    assert a["choch"] is False
    assert a["mss"] is False


# ------------------------------------------------------ internal/external structure


def test_internal_and_external_structure():
    a = analyzer().analyze("T", df_from(STRUCTURE))
    assert a["dealing_range"]["high"] == 13.3
    assert a["dealing_range"]["low"] == 11.9
    internal = {(x["index"], x["side"]) for x in a["internal_structure"]}
    external = {(x["index"], x["side"]) for x in a["external_structure"]}
    assert (17, "high") in internal
    assert (19, "low") in internal
    assert (2, "high") in external
    assert (10, "low") in external
    for x in a["internal_structure"] + a["external_structure"]:
        assert {"index", "price", "side", "location"} <= set(x)


# ------------------------------------------------- non-repainting & fail-open


def test_non_repainting_confirmed_swings():
    short = df_from(ZIGZAG)
    extended = df_from(ZIGZAG + [9.5, 10.0, 10.5, 11.0, 11.5])
    base = find_swings(short, strength=2)
    grown = find_swings(extended, strength=2)
    cutoff = len(short) - 2
    grown_conf = {s["index"]: s["price"] for s in grown["high"] + grown["low"] if s["confirmed"] and s["index"] < cutoff}
    base_conf = {s["index"]: s["price"] for s in base["high"] + base["low"] if s["confirmed"]}
    assert base_conf == grown_conf


def test_insufficient_data_neutral():
    a = analyzer().analyze("T", make_df([100.0] * 5, high_offset=0.5, low_offset=0.5))
    assert a["bos"] is False
    assert a["choch"] is False
    assert a["mss"] is False
    assert a["trend"] == "neutral"
    assert a["confidence"] == 0.5
    assert a["reason"] == "insufficient_data"


def test_neutral_result_never_raises():
    r = neutral_result("T", "boom")
    assert r["bos"] is False
    assert r["confidence"] == 0.5


def test_confidence_within_bounds_when_event():
    for close in (BULL_BOS, BEAR_BOS, CHOCK, MSSK):
        a = analyzer().analyze("T", df_from(close))
        if a["bos"] or a["choch"] or a["mss"]:
            assert 0.5 <= a["confidence"] <= 0.97


# ------------------------------------------------------------- feature wrapper


def test_feature_wrapper_signal_follows_shift():
    f = MarketStructureFeature(None, {})
    fr = f.compute("T", df_from(CHOCK))
    assert fr.name == "market_structure"
    assert fr.signal == "bearish"
    assert 0.5 <= fr.confidence <= 1.0
    assert REQUIRED_KEYS <= set(fr.metadata)


def test_feature_wrapper_signal_follows_trend_when_no_shift():
    f = MarketStructureFeature(None, {})
    assert f.compute("T", df_from(BULL_BOS)).signal == "bullish"
    assert f.compute("T", df_from(ZIGZAG)).signal == "neutral"


def test_feature_wrapper_df_only_and_fail_open():
    assert MarketStructureFeature.df_only is True
    assert MarketStructureFeature.opt_in is True
    fr = MarketStructureFeature(None, {}).compute("T", make_df([100.0] * 4))
    assert fr.signal == "neutral"
    assert fr.is_neutral


def test_feature_is_registered_opt_in():
    names = [f.name for f in FEATURE_REGISTRY]
    assert "market_structure" in names
    engine = build_feature_engine(market_data=None, config={}, enabled=["market_structure"])
    assert engine.has_feature("market_structure")
    default = FeatureEngine(market_data=None, config={})
    assert not default.has_feature("market_structure")


def test_feature_included_in_df_features_when_enabled():
    config = {"features": {"enabled": ["trend", "market_structure"]}}
    fs = compute_df_features("T", df_from(BULL_BOS), config)
    assert "market_structure" in fs
    assert fs["market_structure"].metadata["bos"] is True
