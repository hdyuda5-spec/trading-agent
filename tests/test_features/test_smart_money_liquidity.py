"""Deterministic Smart Money liquidity analyzer tests.

Covers the output schema, swing/equal detection, buy/sell-side liquidity,
internal/external classification, liquidity sweeps + stop hunts, determinism,
the non-repainting guarantee, and the Feature Engine wrapper.
"""

import numpy as np
import pytest

from agent.features import (
    FEATURE_REGISTRY,
    FeatureEngine,
    build_feature_engine,
    compute_df_features,
)
from agent.features.liquidity import (
    SmartMoneyLiquidityAnalyzer,
    SmartMoneyLiquidityFeature,
)
from agent.features.liquidity.smart_money import (
    Config,
    find_swings,
    neutral_result,
)

from tests.conftest import make_df

REQUIRED_KEYS = {
    "buy_side_liquidity",
    "sell_side_liquidity",
    "liquidity_sweep",
    "sweep_direction",
    "confidence",
}

ZIGZAG = [10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10]


def zigzag_df(extra_bars=None):
    close = list(ZIGZAG) + (extra_bars or [])
    return make_df(close, high_offset=0.5, low_offset=0.5)


def analyzer(config=None):
    return SmartMoneyLiquidityAnalyzer(config or {})


def set_last(df, open_, high, low, close, volume=None):
    last = df.index[-1]
    df.loc[last, ["open", "high", "low", "close"]] = [open_, high, low, close]
    if volume is not None:
        df.loc[last, "volume"] = volume
    return df


def bullish_sweep_df():
    df = zigzag_df([9.5])
    return set_last(df, 9.8, 10.5, 6.5, 9.5)


def bearish_sweep_df():
    close = [10, 9, 8, 9, 10, 11, 12, 11, 10, 9, 8, 7, 8, 9, 10, 9, 8, 8.5]
    df = make_df(close, high_offset=0.5, low_offset=0.5)
    return set_last(df, 8.2, 13.5, 7.5, 8.2)


def equal_low_sweep_df():
    close = [10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9.05, 8.95, 10, 11, 9.9, 10.2]
    df = make_df(close, high_offset=0.5, low_offset=0.5)
    return set_last(df, 10.0, 10.8, 8.0, 10.0)


# --------------------------------------------------------------------- swings


def test_swing_highs_and_lows_detected():
    sw = find_swings(zigzag_df(), strength=2)
    confirmed_h = [(s["index"], s["price"]) for s in sw["high"] if s["confirmed"]]
    confirmed_l = [(s["index"], s["price"]) for s in sw["low"] if s["confirmed"]]
    assert confirmed_h == [(2, 12.5), (9, 13.5)]
    assert confirmed_l == [(5, 8.5), (14, 7.5)]


def test_confirmed_pivots_need_right_window():
    df = zigzag_df()
    n = len(df)
    sw = find_swings(df, strength=2)
    for pivot in sw["high"] + sw["low"]:
        if pivot["confirmed"]:
            assert pivot["index"] + 2 < n


def test_trailing_pivots_marked_unconfirmed():
    sw = find_swings(zigzag_df(), strength=2)
    candidates = [s for s in sw["high"] + sw["low"] if not s["confirmed"]]
    assert all(s["index"] >= len(ZIGZAG) - 2 for s in candidates)


def test_equal_high_detected():
    close = [10, 11, 12, 11, 10, 9, 10, 11, 12.03, 11, 10, 9, 10, 11, 10.5, 10.8]
    df = make_df(close, high_offset=0.5, low_offset=0.5)
    a = analyzer({"smart_money_liquidity": {"equal_tolerance_pct": 0.5}}).analyze("T", df)
    eq = a["equal_highs"]
    assert len(eq) == 1
    assert eq[0]["touches"] == 2
    assert eq[0]["source"] == "equal_high"


# -------------------------------------------------------------- liquidity pools


def test_buy_and_sell_side_liquidity():
    a = analyzer().analyze("T", zigzag_df())
    assert {lv["side"] for lv in a["buy_side_liquidity"]} == {"buy"}
    assert {lv["side"] for lv in a["sell_side_liquidity"]} == {"sell"}
    assert {lv["source"] for lv in a["buy_side_liquidity"]} <= {"swing_high", "equal_high"}
    assert {lv["source"] for lv in a["sell_side_liquidity"]} <= {"swing_low", "equal_low"}
    for lv in a["buy_side_liquidity"] + a["sell_side_liquidity"]:
        assert {"price", "side", "source", "touches", "index", "location"} <= set(lv)


def test_external_liquidity_beyond_dealing_range():
    close = [10, 12, 13, 12, 11, 10, 9, 8, 9, 10, 11, 12, 11, 10, 9, 8.5, 9.5, 10.5]
    df = make_df(close, high_offset=0.5, low_offset=0.5)
    a = analyzer().analyze("T", df)
    assert a["dealing_range"]["high"] == 12.5
    assert a["dealing_range"]["low"] == 8.0
    external = {(lv["price"], lv["side"]) for lv in a["external_liquidity"]}
    assert (13.5, "buy") in external
    assert (7.5, "sell") in external
    internal_prices = {(lv["price"], lv["side"]) for lv in a["internal_liquidity"]}
    assert (12.5, "buy") in internal_prices
    assert (8.0, "sell") in internal_prices


# --------------------------------------------------------------------- sweeps


def test_bullish_sweep_detected():
    a = analyzer().analyze("T", bullish_sweep_df())
    assert a["liquidity_sweep"] is True
    assert a["sweep_direction"] == "bullish"
    assert a["stop_hunt"] is False
    assert 0.5 <= a["confidence"] <= 0.97


def test_bearish_sweep_detected():
    a = analyzer().analyze("T", bearish_sweep_df())
    assert a["liquidity_sweep"] is True
    assert a["sweep_direction"] == "bearish"


def test_stop_hunt_on_equal_level():
    cfg = {"smart_money_liquidity": {"equal_tolerance_pct": 1.5}}
    a = analyzer(cfg).analyze("T", equal_low_sweep_df())
    assert a["equal_lows"]
    assert a["liquidity_sweep"] is True
    assert a["sweep_direction"] == "bullish"
    assert a["stop_hunt"] is True
    assert a["sweep"]["level"]["touches"] >= 2


def test_no_sweep_without_rejection():
    a = analyzer().analyze("T", zigzag_df())
    assert a["liquidity_sweep"] is False
    assert a["sweep_direction"] is None
    assert a["stop_hunt"] is False
    assert a["confidence"] == 0.5


def test_sweep_needs_reclaim_above_level():
    # wick pierces the low but the close stays below it -> not a sweep
    df = zigzag_df([7.0])
    df = set_last(df, 7.0, 7.4, 6.5, 7.0)
    a = analyzer().analyze("T", df)
    assert a["liquidity_sweep"] is False


# ------------------------------------------------------- determinism & schema


def test_deterministic_across_instances():
    df = bullish_sweep_df()
    first = analyzer().analyze("T", df)
    second = analyzer().analyze("T", df)
    assert first == second


def test_output_schema():
    a = analyzer().analyze("T", bullish_sweep_df())
    assert REQUIRED_KEYS <= set(a)
    assert isinstance(a["buy_side_liquidity"], list)
    assert isinstance(a["sell_side_liquidity"], list)
    assert isinstance(a["liquidity_sweep"], bool)
    assert isinstance(a["confidence"], float)


def test_non_repainting_confirmed_pivots():
    short = zigzag_df()
    extended = zigzag_df([9.5, 10.0, 10.5, 11.0, 11.5])
    base = find_swings(short, strength=2)
    grown = find_swings(extended, strength=2)
    cutoff = len(short) - 2
    grown_confirmed = [
        s for s in grown["high"] + grown["low"] if s["confirmed"] and s["index"] < cutoff
    ]
    base_confirmed = [s for s in base["high"] + base["low"] if s["confirmed"] and s["index"] < cutoff]
    assert {s["index"] for s in base_confirmed} == {s["index"] for s in grown_confirmed}
    for s in grown_confirmed:
        base_match = [b for b in base_confirmed if b["index"] == s["index"]]
        assert base_match and base_match[0]["price"] == s["price"]


def test_non_repainting_levels_on_shared_region():
    from agent.features.liquidity.smart_money import build_levels

    short = zigzag_df()
    extended = zigzag_df([9.5, 10.0, 10.5, 11.0, 11.5])
    cfg = Config()
    cutoff = len(short) - 2
    short_levels = build_levels(short, find_swings(short, 2), cfg)
    grown_levels = build_levels(extended, find_swings(extended, 2), cfg)
    key = lambda lv: (round(lv["price"], 8), lv["side"])
    short_set = {key(lv) for lv in short_levels["buy"] + short_levels["sell"] if lv["index"] < cutoff}
    grown_set = {key(lv) for lv in grown_levels["buy"] + grown_levels["sell"] if lv["index"] < cutoff}
    assert short_set == grown_set


def test_insufficient_data_neutral():
    df = make_df([100.0] * 5, high_offset=0.5, low_offset=0.5)
    a = analyzer().analyze("T", df)
    assert a["liquidity_sweep"] is False
    assert a["confidence"] == 0.5
    assert a["reason"] == "insufficient_data"


def test_neutral_result_never_raises():
    r = neutral_result("T", "boom")
    assert r["liquidity_sweep"] is False


# ------------------------------------------------------------- feature wrapper


def test_feature_wrapper_result():
    f = SmartMoneyLiquidityFeature(None, {})
    fr = f.compute("T", bullish_sweep_df())
    assert fr.name == "smart_money_liquidity"
    assert fr.signal == "bullish"
    assert 0.5 <= fr.confidence <= 1.0
    assert REQUIRED_KEYS <= set(fr.metadata)


def test_feature_wrapper_neutral_without_sweep():
    f = SmartMoneyLiquidityFeature(None, {})
    fr = f.compute("T", zigzag_df())
    assert fr.signal == "neutral"
    assert fr.metadata["liquidity_sweep"] is False


def test_feature_wrapper_df_only_and_fail_open():
    assert SmartMoneyLiquidityFeature.df_only is True
    fr = SmartMoneyLiquidityFeature(None, {}).compute("T", make_df([100.0] * 4))
    assert fr.signal == "neutral"
    assert fr.is_neutral


def test_feature_is_registered_opt_in():
    names = [f.name for f in FEATURE_REGISTRY]
    assert "smart_money_liquidity" in names
    engine = build_feature_engine(market_data=None, config={}, enabled=["smart_money_liquidity"])
    assert engine.has_feature("smart_money_liquidity")
    default = FeatureEngine(market_data=None, config={})
    assert not default.has_feature("smart_money_liquidity")


def test_feature_included_in_df_features_when_enabled():
    config = {"features": {"enabled": ["trend", "smart_money_liquidity"]}}
    fs = compute_df_features("T", zigzag_df(), config)
    assert "smart_money_liquidity" in fs
    assert fs["smart_money_liquidity"].metadata["liquidity_sweep"] is False
