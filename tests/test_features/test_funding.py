"""FundingFeature: contrarian funding-rate signal, fail-open."""

from agent.features import FundingFeature

from tests.conftest import FakeMarketData, make_df


def funding_feature(market_data, config=None):
    return FundingFeature(market_data, config or {})


def test_high_funding_bearish(config):
    md = FakeMarketData(funding_rate=0.0002)
    fr = funding_feature(md, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.signal == "bearish"
    assert fr.metadata["funding_rate"] == 0.0002


def test_negative_funding_bullish(config):
    md = FakeMarketData(funding_rate=-0.0002)
    fr = funding_feature(md, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.signal == "bullish"


def test_neutral_small_funding(config):
    md = FakeMarketData(funding_rate=0.00002)
    fr = funding_feature(md, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.signal == "neutral"


def test_none_rate_fail_open(config):
    md = FakeMarketData(funding_rate=None)
    fr = funding_feature(md, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral


def test_no_funding_method_fail_open(config):
    class NoFundingMD(FakeMarketData):
        def fetch_funding_rate(self, symbol):
            raise AttributeError

    fr = funding_feature(NoFundingMD(), config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral


def test_no_market_data_fail_open(config):
    fr = funding_feature(None, config).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.is_neutral


def test_custom_extreme_rate(config):
    cfg = dict(config)
    cfg["features"] = {"funding": {"extreme_rate": 0.001}}
    md = FakeMarketData(funding_rate=0.0002)
    fr = funding_feature(md, cfg).compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.signal == "neutral"
