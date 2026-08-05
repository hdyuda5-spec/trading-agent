"""LiquidityFeature: order-book spread/depth, fail-open degradation."""

from agent.features import LiquidityFeature

from tests.conftest import FakeMarketData, make_df


def liquidity_feature(market_data, config=None):
    return LiquidityFeature(market_data, config or {})


def test_tight_spread_liquid(config):
    md = FakeMarketData(order_book={"bids": [[100.0, 5.0]], "asks": [[100.01, 5.0]]})
    fr = liquidity_feature(md, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.signal == "liquid"
    assert fr.metadata["cost_ok"] is True
    assert 0 < fr.metadata["spread_pct"] <= 0.15


def test_wide_spread_illiquid(config):
    md = FakeMarketData(order_book={"bids": [[100.0, 5.0]], "asks": [[101.0, 5.0]]})
    fr = liquidity_feature(md, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.signal == "illiquid"
    assert fr.metadata["cost_ok"] is False


def test_no_market_data_fail_open(config):
    fr = liquidity_feature(None, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral


def test_market_data_error_fail_open(config):
    class BoomMD(FakeMarketData):
        def fetch_order_book(self, symbol, limit=5):
            raise ConnectionError("down")

    fr = liquidity_feature(BoomMD(), config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral


def test_imbalance_and_depth_metadata(config):
    md = FakeMarketData(order_book={"bids": [[100.0, 10.0]], "asks": [[100.05, 2.0]]})
    fr = liquidity_feature(md, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.metadata["imbalance"] > 0
    assert fr.metadata["bid_depth_usdt"] > fr.metadata["ask_depth_usdt"]


def test_empty_book_neutral(config):
    md = FakeMarketData(order_book={"bids": [], "asks": []})
    fr = liquidity_feature(md, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral
    assert fr.metadata["spread_pct"] is None


def test_cache_ttl_default():
    assert LiquidityFeature.cache_ttl > 0
