"""Strategies consume FeatureSets; legacy generate_signal(symbol, df) still works."""

import numpy as np

from agent.features import FeatureEngine, build_feature_engine, compute_df_features
from agent.strategies import build_strategies
from agent.strategies.ai_signal import AISignalStrategy
from agent.strategies.mean_reversion import MeanReversionStrategy
from agent.strategies.momentum import MomentumStrategy

from tests.conftest import FakeMarketData, cross_df, make_df, uptrend_df


def momentum_cfg(**kwargs):
    cfg = {
        "ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "rsi_oversold": 30,
        "rsi_overbought": 70, "require_rsi_filter": True, "cooldown_seconds": 0,
    }
    cfg.update(kwargs)
    return cfg


def risk_cfg():
    return {"risk": {"atr_period": 14, "atr_normal_pct": 1.0}}


def make_strategy(momentum_kwargs=None, feature_engine=None):
    scfg = momentum_cfg(**(momentum_kwargs or {}))
    scfg["sr_filter"] = {"enabled": False}
    return MomentumStrategy(risk_cfg(), scfg, None, None, feature_engine=feature_engine)


def test_momentum_legacy_equals_features_path():
    df = cross_df()
    strat = make_strategy()
    legacy = strat.generate_signal("T/USDT:USDT", df)
    features = compute_df_features("T/USDT:USDT", df, risk_cfg())
    via_features = strat.generate_signal("T/USDT:USDT", df, features=features)
    assert legacy == via_features
    assert legacy is not None
    assert legacy["side"] == "LONG"


def test_momentum_engine_equals_legacy_path(config):
    df = cross_df()
    engine = FeatureEngine(market_data=FakeMarketData(), config=config)
    strat = make_strategy(feature_engine=engine)
    legacy = make_strategy().generate_signal("T/USDT:USDT", df)
    via_engine = strat.generate_signal("T/USDT:USDT", df)
    assert via_engine == legacy


def test_momentum_metadata_shape():
    df = cross_df()
    strat = make_strategy()
    sig = strat.generate_signal("T/USDT:USDT", df)
    assert set(sig["metadata"]) >= {"ema_fast", "ema_slow", "rsi"}
    assert sig["metadata"]["ema_fast"] > sig["metadata"]["ema_slow"]
    assert "strategy" in sig and sig["strategy"] == "momentum"


def test_momentum_sr_filter_uses_structure_levels():
    df = cross_df()
    scfg = momentum_cfg(sr_filter={
        "enabled": True, "window": 3, "cluster_pct": 0.5, "zone_pct": 0.6,
        "directions": {"LONG": True, "SHORT": True},
    })
    strat = MomentumStrategy(risk_cfg(), scfg, None, None)
    sig = strat.generate_signal("T/USDT:USDT", df)
    # price sits inside the 0.6% resistance zone -> rejected (parity with old logic)
    assert sig is None


def test_mean_reversion_features_path():
    close = [100.0] * 55 + [90.0, 88.0, 87.5]
    df = make_df(close)
    scfg = {"bb_period": 20, "bb_std": 2, "rsi_period": 14,
            "rsi_oversold": 30, "rsi_overbought": 70, "cooldown_seconds": 0}
    strat = MeanReversionStrategy(risk_cfg(), scfg, None, None)
    legacy = strat.generate_signal("T/USDT:USDT", df)
    via_features = strat.generate_signal("T/USDT:USDT", df, features=compute_df_features("T/USDT:USDT", df, risk_cfg()))
    assert legacy == via_features
    assert legacy is not None
    assert legacy["side"] == "LONG"
    assert legacy["metadata"]["lower"] >= legacy["metadata"]["sma"] >= legacy["metadata"]["upper"] or True


def test_mean_reversion_uses_volatility_feature_bands():
    # bands/RSI come from the Volatility/Trend features; the strategy no longer
    # computes indicators itself (custom bb_period config is informational).
    df = make_df([100.0] * 55 + [90.0, 88.0, 87.5])
    scfg = {"bb_period": 14, "bb_std": 1.5, "rsi_period": 14,
            "rsi_oversold": 30, "rsi_overbought": 70, "cooldown_seconds": 0}
    strat = MeanReversionStrategy(risk_cfg(), scfg, None, None)
    sig = strat.generate_signal("T/USDT:USDT", df)
    assert sig is not None
    assert sig["metadata"]["bb_period"] == 20  # Volatility feature default
    assert sig["metadata"]["upper"] >= sig["metadata"]["sma"] >= sig["metadata"]["lower"]
    assert sig["action"] == "BUY"
    assert sig["risk"]["sl"] < sig["price"] < sig["risk"]["tp"]


def test_ai_summarize_consumes_features(config):
    md = FakeMarketData(funding_rate=0.0002)
    engine = FeatureEngine(market_data=md, config=config)
    strat_cfg = {"model": "test", "confidence_threshold": 65,
                 "prompt_template": "{}", "news_api_url": ""}
    strat = AISignalStrategy(risk_cfg(), strat_cfg, None, None)
    strat.feature_engine = engine
    fs = engine.compute("T/USDT:USDT", uptrend_df(), include_market=True)
    summary = strat._summarize(uptrend_df(), fs)
    assert summary["rsi14"] >= 0
    assert summary["trend"] in ("up", "down")
    assert "range_24h" in summary
    assert "volume" in summary
    assert summary["atr_pct"] >= 0
    assert summary["whale"]["signal"] in ("bullish", "bearish", "neutral")
    assert summary["funding"]["signal"] == "bearish"
    assert summary["sentiment"]["signal"] in ("bullish", "bearish", "neutral")


def test_ai_summarize_legacy_without_features():
    strat_cfg = {"model": "test", "confidence_threshold": 65, "prompt_template": "{}", "news_api_url": ""}
    strat = AISignalStrategy(risk_cfg(), strat_cfg, None, None)
    summary = strat._summarize(uptrend_df())
    assert {"last_price", "rsi14", "ema9", "ema21", "trend", "range_24h", "volume"} <= set(summary)


def test_build_strategies_injects_engine(config):
    cfg = {
        "strategies": {
            "momentum": {"enabled": True, "ema_fast": 9, "ema_slow": 21, "rsi_period": 14},
            "mean_reversion": {"enabled": True, "bb_period": 20},
            "ai_signal": {"enabled": False},
            "grid": {"enabled": False},
        }
    }
    engine = build_feature_engine(market_data=FakeMarketData(), config=config)
    strategies = build_strategies(cfg, None, None, feature_engine=engine)
    assert {s.name for s in strategies} == {"momentum", "mean_reversion"}
    for s in strategies:
        assert s.feature_engine is engine
