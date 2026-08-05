"""WhaleFeature: aggregate large-trader flow, fail-open degradation."""

import time

from agent.features import WhaleFeature

from tests.conftest import FakeMarketData, make_df


def whale_feature(market_data, config=None):
    return WhaleFeature(market_data, config or {})


def _trade(amount, price, side, ts_ms=None):
    return {"timestamp": ts_ms or int(time.time() * 1000), "amount": amount, "price": price, "side": side}


def test_bullish_net_buy(config):
    trades = [_trade(100, 100, "buy"), _trade(40, 100, "sell")]
    fr = whale_feature(FakeMarketData(trades=trades), config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.signal == "bullish"
    assert fr.metadata["net_usdt"] > 0
    assert fr.metadata["n"] >= 1
    assert "symbol" not in fr.metadata


def test_bearish_net_sell(config):
    trades = [_trade(40, 100, "buy"), _trade(100, 100, "sell")]
    fr = whale_feature(FakeMarketData(trades=trades), config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.signal == "bearish"
    assert fr.metadata["net_usdt"] < 0


def test_no_whale_trades_neutral(config):
    trades = [_trade(1, 100, "buy")]
    fr = whale_feature(FakeMarketData(trades=trades), config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral
    assert fr.metadata["n"] == 0


def test_no_market_data_fail_open(config):
    fr = whale_feature(None, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral


def test_config_min_notional(config):
    trades = [_trade(30, 100, "buy")]  # 3000 USDT
    fr = whale_feature(FakeMarketData(trades=trades), config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral  # below 5000 min_notional


def test_old_trades_ignored(config):
    old = int(time.time() * 1000) - 100 * 60 * 1000
    trades = [_trade(100, 100, "buy", ts_ms=old)]
    fr = whale_feature(FakeMarketData(trades=trades), config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral
