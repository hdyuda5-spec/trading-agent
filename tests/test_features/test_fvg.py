"""Deterministic Fair Value Gap detector tests.

Covers the required schema (direction/high/low/filled), bullish + bearish
FVGs, unmitigated / mitigated / filled / invalidated status, configurable
minimum gap size, multi-timeframe support, determinism, fail-open behavior,
and the Feature Engine wrapper.
"""

import pytest

from agent.features import (
    FEATURE_REGISTRY,
    FeatureEngine,
    build_feature_engine,
    compute_df_features,
)
from agent.features.fvg import FairValueGapAnalyzer, FairValueGapFeature
from agent.features.fvg.analyzer import (
    Config,
    detect_fvgs,
    neutral_result,
)

from tests.conftest import make_df

REQUIRED_KEYS = {"direction", "high", "low", "filled"}
STATUSES = {"unmitigated", "mitigated", "filled", "invalidated"}

BULLISH_UNMIT = [10, 10.5, 12, 12.5, 13, 13.5, 14]
MITIGATED = [10, 10.5, 12, 12.5, 13, 11.4, 12]
INVALIDATED = [10, 10.5, 12, 12.5, 13, 11, 10.2]
BEARISH_UNMIT = [14, 13.5, 12, 11.5, 11, 10.5, 10]
BEARISH_MITIGATED = [14, 13.5, 12, 11.5, 11, 12.6, 12]
BEARISH_INVALIDATED = [14, 13.5, 12, 11.5, 11, 13, 13.8]


def df_from(close):
    return make_df(close, high_offset=0.5, low_offset=0.5)


def analyzer(config=None):
    return FairValueGapAnalyzer(config or {})


def gap_by_index(result, index):
    matches = [g for g in result["fvg"] if g["index"] == index]
    assert matches, f"no fvg at index {index}: {result['fvg']}"
    return matches[0]


# ---------------------------------------------------------------------- schema


def test_required_schema_present():
    a = analyzer().analyze("T", df_from(BULLISH_UNMIT))
    assert "fvg" in a
    assert a["fvg"]
    for g in a["fvg"]:
        assert REQUIRED_KEYS <= set(g)
        assert g["direction"] in ("bullish", "bearish")
        assert isinstance(g["high"], float)
        assert isinstance(g["low"], float)
        assert isinstance(g["filled"], bool)
        assert g["high"] >= g["low"]


def test_deterministic_across_instances():
    df = df_from(MITIGATED)
    assert analyzer().analyze("T", df) == analyzer().analyze("T", df)


def test_all_floats_are_python_float():
    for g in analyzer().analyze("T", df_from(MITIGATED))["fvg"]:
        assert type(g["high"]) is float
        assert type(g["low"]) is float
        assert type(g["gap"]) is float
        assert type(g["filled_ratio"]) is float


# ------------------------------------------------------------------- directions


def test_bullish_fvg_detected():
    a = analyzer().analyze("T", df_from(BULLISH_UNMIT))
    g = gap_by_index(a, 1)
    assert g["direction"] == "bullish"
    assert g["low"] == 10.5
    assert g["high"] == 11.5
    assert g["status"] == "unmitigated"
    assert g["filled"] is False


def test_bearish_fvg_detected():
    a = analyzer().analyze("T", df_from(BEARISH_UNMIT))
    g = gap_by_index(a, 1)
    assert g["direction"] == "bearish"
    assert g["low"] == 12.5
    assert g["high"] == 13.5
    assert g["status"] == "unmitigated"
    assert g["filled"] is False


# ---------------------------------------------------------------------- status


def test_unmitigated_status():
    a = analyzer().analyze("T", df_from(BULLISH_UNMIT))
    assert all(g["status"] == "unmitigated" for g in a["fvg"])
    assert all(g["filled"] is False for g in a["fvg"])
    assert all(g["filled_ratio"] == 0.0 for g in a["fvg"])


def test_mitigated_partial_fill():
    a = analyzer().analyze("T", df_from(MITIGATED))
    g = gap_by_index(a, 1)
    assert g["status"] == "mitigated"
    assert g["filled"] is False
    assert g["filled_ratio"] == 0.6


def test_filled_status():
    a = analyzer().analyze("T", df_from(MITIGATED))
    g = gap_by_index(a, 2)
    assert g["status"] == "filled"
    assert g["filled"] is True
    assert g["filled_ratio"] == 1.0


def test_invalidated_bullish():
    a = analyzer().analyze("T", df_from(INVALIDATED))
    g = gap_by_index(a, 1)
    assert g["status"] == "invalidated"
    assert g["filled"] is True


def test_mitigated_bearish():
    a = analyzer().analyze("T", df_from(BEARISH_MITIGATED))
    g = gap_by_index(a, 1)
    assert g["direction"] == "bearish"
    assert g["status"] == "mitigated"
    assert g["filled"] is False
    assert g["filled_ratio"] == 0.6


def test_invalidated_bearish():
    a = analyzer().analyze("T", df_from(BEARISH_INVALIDATED))
    g = gap_by_index(a, 1)
    assert g["direction"] == "bearish"
    assert g["status"] == "invalidated"
    assert g["filled"] is True


def test_status_subset():
    a = analyzer().analyze("T", df_from(MITIGATED))
    assert {g["status"] for g in a["fvg"]} <= STATUSES


# ---------------------------------------------------------------- minimum gap


def test_min_gap_pct_filters_small_gaps():
    close = [10, 10.2, 11.3, 11.5, 11.8]
    df = df_from(close)
    default = analyzer().analyze("T", df)
    assert default["fvg"], "expected a gap with default min gap"
    strict = analyzer({"fvg": {"min_gap_pct": 5.0}}).analyze("T", df)
    assert strict["fvg"] == []


def test_min_gap_pct_keeps_large_gaps():
    close = [10, 10.2, 11.3, 11.5, 11.8]
    df = df_from(close)
    lenient = analyzer({"fvg": {"min_gap_pct": 1.0}}).analyze("T", df)
    assert lenient["fvg"], "gap of ~2.8% should survive a 1% threshold"


# ---------------------------------------------------------------- multi-timeframe


def test_multi_timeframe_analysis():
    a = analyzer()
    result = a.analyze_timeframes(
        "T",
        {
            "5m": df_from(BULLISH_UNMIT),
            "1h": df_from(BEARISH_UNMIT),
        },
    )
    assert set(result) == {"5m", "1h"}
    assert result["5m"]["fvg"] and result["5m"]["fvg"][0]["direction"] == "bullish"
    assert result["1h"]["fvg"] and result["1h"]["fvg"][0]["direction"] == "bearish"
    for tf, res in result.items():
        for g in res["fvg"]:
            assert g["timeframe"] == tf
            assert res["timeframe"] == tf


# ---------------------------------------------------------------- fail-open


def test_insufficient_data_neutral():
    a = analyzer().analyze("T", make_df([100.0] * 3, high_offset=0.5, low_offset=0.5))
    assert a["fvg"] == []
    assert a["count"] == 0
    assert a["reason"] == "insufficient_data"


def test_neutral_result_never_raises():
    r = neutral_result("T", "5m", "boom")
    assert r["fvg"] == []
    assert r["count"] == 0


# ------------------------------------------------------------- feature wrapper


def test_feature_wrapper_signal_bullish():
    f = FairValueGapFeature(None, {})
    fr = f.compute("T", df_from(BULLISH_UNMIT))
    assert fr.name == "fvg"
    assert fr.signal == "bullish"
    assert 0.5 <= fr.confidence <= 1.0
    assert "fvg" in fr.metadata
    assert fr.metadata["latest_unmitigated"]["direction"] == "bullish"


def test_feature_wrapper_signal_neutral_without_recent_gap():
    f = FairValueGapFeature(None, {})
    fr = f.compute("T", df_from([10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]))
    assert fr.signal == "neutral"
    assert fr.is_neutral


def test_feature_wrapper_df_only_and_opt_in():
    assert FairValueGapFeature.df_only is True
    assert FairValueGapFeature.opt_in is True
    fr = FairValueGapFeature(None, {}).compute("T", make_df([100.0] * 3))
    assert fr.signal == "neutral"
    assert fr.is_neutral


def test_feature_is_registered_opt_in():
    names = [f.name for f in FEATURE_REGISTRY]
    assert "fvg" in names
    engine = build_feature_engine(market_data=None, config={}, enabled=["fvg"])
    assert engine.has_feature("fvg")
    default = FeatureEngine(market_data=None, config={})
    assert not default.has_feature("fvg")


def test_feature_included_in_df_features_when_enabled():
    config = {"features": {"enabled": ["trend", "fvg"]}}
    fs = compute_df_features("T", df_from(BULLISH_UNMIT), config)
    assert "fvg" in fs
    assert fs["fvg"].metadata["fvg"]
