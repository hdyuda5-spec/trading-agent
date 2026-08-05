"""FeatureEngine: orchestration, caching, df-only mode, immutability."""

import time

import pytest

from agent.features import (
    DF_ONLY_NAMES,
    FeatureEngine,
    FeatureResult,
    FeatureSet,
    build_feature_engine,
    compute_df_features,
)

from tests.conftest import FakeMarketData, make_df, uptrend_df

ALL_FEATURES = {"trend", "volatility", "volume", "liquidity", "structure", "whale", "sentiment", "funding"}


def test_compute_df_features_no_network():
    fs = compute_df_features("T/USDT:USDT", uptrend_df())
    assert set(fs.names()) == ALL_FEATURES
    assert fs.trend.signal == "bullish"
    # market-dependent features degrade to neutral in df-only mode
    for name in ("liquidity", "whale", "sentiment", "funding"):
        assert fs[name].is_neutral, name


def test_schema_invariant():
    fs = compute_df_features("T/USDT:USDT", uptrend_df())
    for name, fr in fs.to_dict().items():
        # canonical shape: {<name>: <signal>, "confidence": <float>, "metadata": {...}}
        assert set(fr) == {name, "confidence", "metadata"}
        assert isinstance(fr[name], str)
        assert isinstance(fr["confidence"], float)
        assert isinstance(fr["metadata"], dict)


def test_df_only_mode_skips_network_calls(config):
    md = FakeMarketData()
    engine = FeatureEngine(market_data=md, config=config)
    engine.compute("T/USDT:USDT", uptrend_df(), include_market=False)
    assert md.calls == {"order_book": 0, "trades": 0, "funding": 0, "ohlcv": 0}


def test_include_market_calls_network(config):
    md = FakeMarketData()
    engine = FeatureEngine(market_data=md, config=config)
    fs = engine.compute("T/USDT:USDT", uptrend_df(), include_market=True)
    assert md.calls["order_book"] == 2  # liquidity + sentiment
    assert md.calls["trades"] == 2  # sentiment cvd + whale
    assert md.calls["funding"] == 1
    assert fs.liquidity.signal == "liquid"
    assert fs.whale.signal in ("bullish", "bearish")
    assert fs.sentiment.signal in ("bullish", "bearish", "neutral")


def test_ttl_cache_avoids_refetch(config):
    md = FakeMarketData()
    engine = FeatureEngine(market_data=md, config=config)
    engine.compute("T/USDT:USDT", uptrend_df(), include_market=True)
    engine.compute("T/USDT:USDT", uptrend_df(), include_market=True)
    assert md.calls["order_book"] == 2
    assert md.calls["funding"] == 1


def test_cache_by_symbol(config):
    md = FakeMarketData()
    engine = FeatureEngine(market_data=md, config=config)
    engine.compute("A/USDT:USDT", uptrend_df(), include_market=True)
    engine.compute("B/USDT:USDT", uptrend_df(), include_market=True)
    assert md.calls["funding"] == 2


def test_cache_respects_ttl_expiry(config):
    md = FakeMarketData()
    engine = FeatureEngine(market_data=md, config=config)
    now = time.time()
    engine.compute("T/USDT:USDT", uptrend_df(), include_market=True, now=now)
    engine.compute("T/USDT:USDT", uptrend_df(), include_market=True, now=now + 999999)
    assert md.calls["funding"] == 2


def test_clear_cache(config):
    md = FakeMarketData()
    engine = FeatureEngine(market_data=md, config=config)
    engine.compute("T/USDT:USDT", uptrend_df(), include_market=True)
    engine.clear_cache()
    engine.compute("T/USDT:USDT", uptrend_df(), include_market=True)
    assert md.calls["funding"] == 2


def test_enabled_whitelist():
    engine = build_feature_engine(market_data=None, config={}, enabled=["trend", "volume"])
    assert set(engine._index) == {"trend", "volume"}


def test_enabled_from_config():
    engine = build_feature_engine(market_data=None, config={"features": {"enabled": ["trend"]}})
    assert set(engine._index) == {"trend"}


def test_failing_feature_fail_open():
    class BoomFeature:
        name = "boom"
        df_only = True
        cache_ttl = 0.0

        def unavailable(self, reason=""):
            return FeatureResult.neutral("boom")

        def compute(self, symbol, df):
            raise RuntimeError("boom")

    engine = FeatureEngine(features=[BoomFeature()])
    fs = engine.compute("T/USDT:USDT", uptrend_df())
    assert fs["boom"].is_neutral


def test_feature_set_immutable():
    fs = compute_df_features("T/USDT:USDT", uptrend_df())
    with pytest.raises(Exception):
        fs.features["trend"] = FeatureResult.neutral("trend")


def test_feature_set_getitem_and_contains():
    fs = compute_df_features("T/USDT:USDT", uptrend_df())
    assert "trend" in fs
    assert fs["trend"].signal == "bullish"
    assert fs.get("missing") is None
    assert fs.get("missing", FeatureResult.neutral("x")).signal == "neutral"


def test_feature_set_snapshot():
    fs = compute_df_features("T/USDT:USDT", uptrend_df())
    snap = fs.snapshot()
    assert snap["trend"] == "bullish"
    assert set(snap) == ALL_FEATURES


def test_feature_set_properties():
    fs = compute_df_features("T/USDT:USDT", uptrend_df())
    assert fs.trend.name == "trend"
    assert fs.volatility.name == "volatility"
    assert fs.volume.name == "volume"
    assert fs.liquidity.name == "liquidity"
    assert fs.structure.name == "structure"
    assert fs.whale.name == "whale"
    assert fs.sentiment.name == "sentiment"
    assert fs.funding.name == "funding"


def test_df_only_names_constant():
    assert DF_ONLY_NAMES == frozenset({"trend", "volatility", "volume", "structure"})


def test_unavailable_result_marker(config):
    engine = FeatureEngine(market_data=None, config=config)
    fs = engine.compute("T/USDT:USDT", uptrend_df(), include_market=False)
    assert fs.liquidity.is_neutral
    assert fs.whale.is_neutral
    assert fs.sentiment.is_neutral
    assert fs.funding.is_neutral
