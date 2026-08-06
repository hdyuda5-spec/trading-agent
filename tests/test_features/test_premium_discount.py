"""Deterministic Premium / Discount Zone analyzer tests.

Covers the requested ``{premium, discount, zone}`` output, equilibrium, the
configurable lookback, latest-swing selection, fail-open behavior,
determinism, and the Feature Engine wrapper (buy in discount, sell in
premium).
"""

import numpy as np
import pytest

from agent.features import (
    FEATURE_REGISTRY,
    FeatureEngine,
    build_feature_engine,
    compute_df_features,
)
from agent.features.premium_discount import (
    PremiumDiscountAnalyzer,
    PremiumDiscountFeature,
)
from agent.features.premium_discount.analyzer import neutral_result

from tests.conftest import make_df
from tests.test_features.test_order_block import UNMIT

PD_DISCOUNT = UNMIT[:-1] + [11.9]  # price 11.9 -> discount 0.28, premium 0.72
PD_PREMIUM = UNMIT  # price 14.5 -> discount 0.8, premium 0.2
PD_EQUIL = UNMIT[:-1] + [13.0]  # price == equilibrium 13.0
PD_DISCOUNT_DEEP = UNMIT[:-1] + [10.6]  # deep discount, high confidence
EXTENDED = UNMIT + [15, 16, 17, 16.5, 16, 15.5, 15, 14.5, 14, 15, 15.5, 16, 16.5]


def df_from(close):
    return make_df(close, high_offset=0.5, low_offset=0.5)


def analyzer(config=None):
    return PremiumDiscountAnalyzer(config or {})


def run(close, config=None):
    return analyzer(config).analyze("T", df_from(close))


# --------------------------------------------------------- requested contract


def test_returns_premium_discount_zone():
    """Exactly the requested shape: premium + discount == 1 and zone label."""
    r = run(PD_DISCOUNT)
    assert r["premium"] == 0.72
    assert r["discount"] == 0.28
    assert r["zone"] == "discount"
    assert r["equilibrium"] == 13.0
    assert r["range"] == 5.0


def test_ratios_always_sum_to_one():
    for close in (PD_DISCOUNT, PD_PREMIUM, PD_EQUIL):
        r = run(close)
        assert r["premium"] + r["discount"] == pytest.approx(1.0)


# ------------------------------------------------------------ zone boundaries


def test_premium_zone_above_equilibrium():
    r = run(PD_PREMIUM)
    assert r["zone"] == "premium"
    assert r["discount"] == 0.8
    assert r["premium"] == 0.2
    assert r["in_range"] is True


def test_equilibrium_zone_at_midpoint():
    r = run(PD_EQUIL)
    assert r["zone"] == "equilibrium"
    assert r["premium"] == 0.5
    assert r["discount"] == 0.5


def test_position_and_indices_exposed():
    r = run(PD_DISCOUNT)
    assert r["position"] == pytest.approx(0.28)
    assert r["price"] == 11.9
    assert r["swing_high"] == 15.5
    assert r["swing_low"] == 10.5
    assert r["swing_high_index"] == 12
    assert r["swing_low_index"] == 8


# --------------------------------------------------------------- lookback


def test_lookback_picks_latest_swing_range():
    r = run(EXTENDED, {"premium_discount": {"lookback": 10}})
    assert r["swing_high"] == 17.5
    assert r["swing_low"] == 13.5
    assert r["range"] == 4.0


def test_lookback_neutral_when_no_recent_swings():
    r = run(EXTENDED, {"premium_discount": {"lookback": 8}})
    assert r["zone"] == "unknown"
    assert r["reason"] == "no_recent_swing_range"


# ---------------------------------------------------------------- fail-open


def test_insufficient_data_neutral():
    r = run([100.0] * 5)
    assert r["zone"] == "unknown"
    assert r["reason"] == "insufficient_data"
    assert r["premium"] is None and r["discount"] is None


def test_no_swings_neutral():
    r = run([float(i) for i in range(20)])
    assert r["zone"] == "unknown"
    assert r["reason"] == "no_recent_swing_range"


def test_neutral_result_never_raises():
    r = neutral_result("T", "15m", "boom")
    assert r["zone"] == "unknown"
    assert r["reason"] == "boom"


def test_analyzer_never_raises_on_garbage():
    assert analyzer().analyze("T", None)["reason"] == "insufficient_data"
    assert analyzer().analyze("T", object())["reason"] == "analysis_failed"


# ---------------------------------------------------------- determinism/types


def test_deterministic_across_instances():
    df = df_from(PD_DISCOUNT)
    assert analyzer().analyze("T", df) == analyzer().analyze("T", df)


def test_float_type_purity():
    r = run(PD_DISCOUNT)
    for key in ("premium", "discount", "equilibrium", "swing_high", "swing_low", "price", "range", "position"):
        assert type(r[key]) is float, key
    for key in ("swing_high_index", "swing_low_index"):
        assert type(r[key]) is int, key


# ---------------------------------------------------------------- multi-tf


def test_multi_timeframe_analysis():
    result = analyzer().analyze_timeframes(
        "T",
        {"5m": df_from(PD_DISCOUNT), "1h": df_from(PD_PREMIUM)},
    )
    assert set(result) == {"5m", "1h"}
    assert result["5m"]["zone"] == "discount"
    assert result["1h"]["zone"] == "premium"
    for tf, res in result.items():
        assert res["timeframe"] == tf


# ------------------------------------------------------------- feature wrapper


def test_wrapper_buys_discount_sells_premium():
    f = PremiumDiscountFeature(None, {})
    discount = f.compute("T", df_from(PD_DISCOUNT))
    assert discount.name == "premium_discount"
    assert discount.signal == "bullish"
    assert discount.metadata["zone"] == "discount"
    assert discount.metadata["premium"] == 0.72
    assert discount.metadata["discount"] == 0.28

    premium = f.compute("T", df_from(PD_PREMIUM))
    assert premium.signal == "bearish"

    equil = f.compute("T", df_from(PD_EQUIL))
    assert equil.signal == "neutral"
    assert equil.is_neutral


def test_wrapper_confidence_deepens_with_discount():
    f = PremiumDiscountFeature(None, {})
    shallow = f.compute("T", df_from(PD_DISCOUNT))
    deep = f.compute("T", df_from(PD_DISCOUNT_DEEP))
    assert deep.confidence > shallow.confidence
    assert deep.confidence > 0.5
    assert 0.5 <= shallow.confidence <= 1.0


def test_wrapper_df_only_opt_in_and_registry():
    assert PremiumDiscountFeature.df_only is True
    assert PremiumDiscountFeature.opt_in is True
    names = [f.name for f in FEATURE_REGISTRY]
    assert "premium_discount" in names
    engine = build_feature_engine(market_data=None, config={}, enabled=["premium_discount"])
    assert engine.has_feature("premium_discount")
    assert not FeatureEngine(market_data=None, config={}).has_feature("premium_discount")


def test_feature_included_in_df_features_when_enabled():
    config = {"features": {"enabled": ["trend", "premium_discount"]}}
    fs = compute_df_features("T", df_from(PD_DISCOUNT), config)
    assert "premium_discount" in fs
    assert fs["premium_discount"].signal == "bullish"
