"""OpenInterestFeature: OI + price direction signal, fail-open."""

from agent.features import OpenInterestFeature

from tests.conftest import make_df


class FakeOIClient:
    def __init__(self, current=None, history=None, fail_open_interest=False, fail_history=False):
        self.current = current
        self.history = history
        self.fail_open_interest = fail_open_interest
        self.fail_history = fail_history

    def fetch_open_interest(self, symbol):
        if self.fail_open_interest:
            raise Exception("oi down")
        return self.current

    def fetch_open_interest_history(self, symbol, timeframe, limit=10):
        if self.fail_history:
            raise Exception("history down")
        return self.history


class FakeMD:
    def __init__(self, client):
        self.client = client


def oi_feature(market_data, config=None):
    return OpenInterestFeature(market_data, config or {})


def rising(close):
    return make_df(close)


def test_oi_rising_price_rising_bullish(config):
    history = [{"openInterestValue": 100.0} for _ in range(5)] + [
        {"openInterestValue": 100.0 + i * 10.0} for i in range(1, 6)
    ]
    md = FakeMD(FakeOIClient(current={"openInterestValue": 200.0}, history=history))
    fr = oi_feature(md, config).compute("T/USDT:USDT", rising([100.0 + i for i in range(60)]))
    assert fr.signal == "bullish"
    assert fr.metadata["open_interest"] == 200.0
    assert fr.metadata["history_samples"] == 10


def test_oi_rising_price_falling_bearish(config):
    history = [{"openInterestAmount": 1000.0} for _ in range(5)] + [
        {"openInterestAmount": 1000.0 + i * 100.0} for i in range(1, 6)
    ]
    md = FakeMD(FakeOIClient(current={"openInterestAmount": 2000.0}, history=history))
    fr = oi_feature(md, config).compute("T/USDT:USDT", rising([100.0 - i for i in range(60)]))
    assert fr.signal == "bearish"


def test_oi_falling_price_rising_bearish(config):
    history = [{"openInterestValue": 200.0 + i * 10.0} for i in range(10, 0, -1)]
    md = FakeMD(FakeOIClient(current={"openInterestValue": 100.0}, history=history))
    fr = oi_feature(md, config).compute("T/USDT:USDT", rising([100.0 + i for i in range(60)]))
    assert fr.signal == "bearish"


def test_oi_flat_neutral(config):
    history = [{"openInterestValue": 100.0} for _ in range(10)]
    md = FakeMD(FakeOIClient(current={"openInterestValue": 100.0}, history=history))
    fr = oi_feature(md, config).compute("T/USDT:USDT", rising([100.0] * 60))
    assert fr.is_neutral


def test_no_client_fail_open(config):
    class NoClientMD:
        pass

    fr = oi_feature(NoClientMD(), config).compute("T/USDT:USDT", rising([100.0] * 60))
    assert fr.is_neutral


def test_no_oi_methods_fail_open(config):
    class NoMethodsClient:
        pass

    fr = oi_feature(FakeMD(NoMethodsClient()), config).compute("T/USDT:USDT", rising([100.0] * 60))
    assert fr.is_neutral


def test_exchange_error_fail_open(config):
    md = FakeMD(FakeOIClient(current={"openInterestValue": 200.0}, history=None, fail_history=True))
    fr = oi_feature(md, config).compute("T/USDT:USDT", rising([100.0] * 60))
    assert fr.signal == "neutral"


def test_no_market_data_fail_open(config):
    fr = oi_feature(None, config).compute("T/USDT:USDT", rising([100.0] * 60))
    assert fr.is_neutral
