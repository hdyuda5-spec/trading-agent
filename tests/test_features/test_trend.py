"""TrendFeature: directional signals, cross events, metadata contract."""

import numpy as np

from agent.features import TrendFeature
from agent.features.base import BaseFeature

from tests.conftest import cross_df, downtrend_df, make_df, uptrend_df


def trend_feature(config=None, **overrides):
    cfg = {"features": {"trend": {"mode": "ema_cross", "ema_fast": 21, "ema_slow": 50, "adx_enabled": False}}}
    if overrides:
        cfg["features"]["trend"].update(overrides)
    return TrendFeature(None, cfg)


def test_df_only_flag():
    assert issubclass(TrendFeature, BaseFeature)
    assert TrendFeature.df_only is True


def test_uptrend_bullish(config):
    fr = trend_feature().compute("T/USDT:USDT", uptrend_df())
    assert fr.signal == "bullish"
    assert fr.confidence >= 0.5


def test_downtrend_bearish():
    fr = trend_feature().compute("T/USDT:USDT", downtrend_df())
    assert fr.signal == "bearish"


def test_flat_neutral():
    fr = trend_feature().compute("T/USDT:USDT", make_df([100.0] * 120))
    assert fr.signal == "neutral"


def test_stack_mode_bullish():
    fr = trend_feature(mode="ema_stack").compute("T/USDT:USDT", uptrend_df())
    assert fr.signal == "bullish"


def test_stack_mode_bearish():
    fr = trend_feature(mode="ema_stack").compute("T/USDT:USDT", downtrend_df())
    assert fr.signal == "bearish"


def test_metadata_contract():
    fr = trend_feature().compute("T/USDT:USDT", uptrend_df())
    expected = {
        "price", "ema9", "ema21", "ema50", "ema_fast", "ema_slow", "rsi",
        "adx", "mode", "ema_fast_period", "ema_slow_period",
        "cross_9_21", "cross_21_50", "cross_9_21_event", "cross_21_50_event",
    }
    assert set(fr.metadata.keys()) >= expected
    assert fr.metadata["mode"] == "ema_cross"
    assert fr.metadata["ema_fast_period"] == 21
    assert fr.metadata["ema_slow_period"] == 50
    assert fr.metadata["rsi"] is not None


def test_cross_event_detected():
    df = cross_df()
    fr = trend_feature(ema_fast=9, ema_slow=21).compute("T/USDT:USDT", df)
    assert fr.metadata["cross_9_21_event"] == "bullish"
    assert fr.signal == "bullish"


def test_custom_periods_metadata():
    fr = trend_feature(ema_fast=9, ema_slow=21).compute("T/USDT:USDT", uptrend_df())
    assert fr.metadata["ema_fast_period"] == 9
    assert fr.metadata["ema_slow_period"] == 21


def test_insufficient_data_neutral():
    fr = trend_feature().compute("T/USDT:USDT", make_df([100.0]))
    assert fr.is_neutral
    assert "insufficient_data" in fr.metadata.get("reason", "")


def test_adx_boost():
    cfg = {"features": {"trend": {"adx_enabled": True, "adx_period": 14, "min_adx": 20}}}
    fr = TrendFeature(None, cfg).compute("T/USDT:USDT", uptrend_df())
    assert fr.confidence >= 0.5
