"""MarketFeed: pure symbol normalization + ws message parsing."""

from agent.services.market_feed import (
    normalize_symbol,
    parse_kline,
    parse_mark_price,
)


class FakeMarkets:
    def __init__(self, markets):
        self.markets = markets


class FakeExchange:
    def __init__(self, markets):
        self.client = FakeMarkets(markets)


class TestNormalizeSymbol:
    def test_plain_usdt(self):
        assert normalize_symbol("BTCUSDT") == "BTC/USDT:USDT"

    def test_lowercase_input(self):
        assert normalize_symbol("bnbusdt") == "BNB/USDT:USDT"

    def test_passthrough_ccxt_symbol(self):
        assert normalize_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"

    def test_swap_symbol_adds_usdt(self):
        assert normalize_symbol("ETH/USDT") == "ETH/USDT:USDT"

    def test_market_table_authoritative(self):
        ex = FakeExchange({"BTC/USDT:USDT": {"id": "BTCUSDT"}})
        assert normalize_symbol("BTCUSDT", ex) == "BTC/USDT:USDT"

    def test_short_symbol_fallback(self):
        assert normalize_symbol("BNB") == "BNB/USDT:USDT"


class TestParseMarkPrice:
    def test_valid(self):
        ev = parse_mark_price({"e": "markPriceUpdate", "s": "BNBUSDT", "p": "500.5", "T": 123})
        assert ev is not None
        assert ev.topic == "market.tick"
        assert ev.source == "feed.ws"
        assert ev.data["symbol"] == "BNB/USDT:USDT"
        assert ev.data["price"] == 500.5
        assert ev.data["mark_price"] == 500.5
        assert ev.data["ts_ms"] == 123

    def test_invalid_price(self):
        assert parse_mark_price({"e": "markPriceUpdate", "s": "BNBUSDT", "p": "abc"}) is None

    def test_zero_price(self):
        assert parse_mark_price({"e": "markPriceUpdate", "s": "BNBUSDT", "p": "0"}) is None

    def test_missing_price(self):
        assert parse_mark_price({"e": "markPriceUpdate", "s": "BNBUSDT"}) is None


class TestParseKline:
    K = {"e": "kline", "s": "BNBUSDT", "k": {
        "t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "x": False, "i": "15m",
    }}

    def test_valid(self):
        ev = parse_kline(self.K)
        assert ev is not None
        assert ev.topic == "market.candle"
        assert ev.data["symbol"] == "BNB/USDT:USDT"
        assert ev.data["timeframe"] == "15m"
        assert ev.data["ts"] == 1000
        assert ev.data["candle"]["closed"] is False
        assert ev.data["candle"]["close"] == 1.5

    def test_closed_flag(self):
        k = dict(self.K)
        k["k"]["x"] = True
        assert parse_kline(k).data["candle"]["closed"] is True

    def test_missing_candle(self):
        assert parse_kline({"e": "kline", "s": "BNBUSDT"}) is None

    def test_invalid_close(self):
        k = dict(self.K)
        k["k"]["c"] = "bad"
        assert parse_kline(k) is None

    def test_zero_ts(self):
        k = dict(self.K)
        k["k"]["t"] = 0
        assert parse_kline(k) is None
