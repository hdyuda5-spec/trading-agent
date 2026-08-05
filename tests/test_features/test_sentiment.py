"""SentimentFeature: smart-money signal via SmartMoneyAnalyzer, fail-open."""

from agent.features import SentimentFeature

from tests.conftest import FakeMarketData, make_df, uptrend_df


def sentiment_feature(market_data, config=None):
    return SentimentFeature(market_data, config or {})


def test_bullish_uptrend(config):
    md = FakeMarketData()
    fr = sentiment_feature(md, config).compute("T/USDT:USDT", uptrend_df())
    # buy-pct 66% (cvd+) + rising OBV (obv+) -> LONG
    assert fr.signal == "bullish"
    assert "score" in fr.metadata
    assert fr.metadata["score"]["direction"] == "LONG"


def test_bearish_downtrend(config):
    from tests.conftest import downtrend_df

    md = FakeMarketData()
    fr = sentiment_feature(md, config).compute("T/USDT:USDT", downtrend_df())
    assert fr.metadata["score"]["direction"] in ("SHORT", "NEUTRAL")


def test_disabled_smart_money_fail_open(config):
    cfg = dict(config)
    cfg["smart_money"] = {"enabled": False}
    md = FakeMarketData()
    fr = sentiment_feature(md, cfg).compute("T/USDT:USDT", uptrend_df())
    assert fr.is_neutral


def test_no_market_data_fail_open(config):
    fr = sentiment_feature(None, config).compute("T/USDT:USDT", uptrend_df())
    assert fr.is_neutral


def test_legacy_metadata_shape(config):
    md = FakeMarketData()
    fr = sentiment_feature(md, config).compute("T/USDT:USDT", uptrend_df())
    assert {"book", "cvd", "obv", "score"} <= set(fr.metadata.keys())
