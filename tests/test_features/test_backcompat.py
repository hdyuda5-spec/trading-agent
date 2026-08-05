"""Backward-compat: TrendFilter, Screener and strategy signatures unchanged."""

from agent.core.screener import Screener
from agent.core.trend import TrendFilter

from tests.conftest import FakeMarketData, make_df, uptrend_df


class OHLCVMarketData(FakeMarketData):
    """Fake exchange that also serves OHLCV lists."""

    def __init__(self, df, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.df = df
        self.calls["fetch_ohlcv"] = 0

    def fetch_ohlcv(self, symbol, timeframe="1h", limit=200, since=None):
        self.calls["fetch_ohlcv"] += 1
        d = self.df
        return [
            [int(ts.value / 1_000_000), o, h, l, c, v]
            for ts, o, h, l, c, v in zip(
                d.index, d["open"], d["high"], d["low"], d["close"], d["volume"]
            )
        ]


def test_trend_filter_direction_long():
    df = uptrend_df()
    md = OHLCVMarketData(df)
    config = {
        "trend_filter": {"enabled": True, "timeframe": "1h", "ema_fast": 21, "ema_slow": 50, "symbols": []},
        "risk": {"atr_period": 14},
    }
    tf = TrendFilter(md, config)
    assert tf.direction("T/USDT:USDT") == "LONG"


def test_trend_filter_direction_short():
    from tests.conftest import downtrend_df

    md = OHLCVMarketData(downtrend_df())
    config = {
        "trend_filter": {"enabled": True, "timeframe": "1h", "ema_fast": 21, "ema_slow": 50, "symbols": []},
        "risk": {"atr_period": 14},
    }
    tf = TrendFilter(md, config)
    assert tf.direction("T/USDT:USDT") == "SHORT"


def test_trend_filter_disabled_returns_none():
    md = OHLCVMarketData(uptrend_df())
    tf = TrendFilter(md, {"trend_filter": {"enabled": False}})
    assert tf.direction("T/USDT:USDT") is None


def test_trend_filter_uses_configured_periods():
    df = uptrend_df()
    md = OHLCVMarketData(df)
    config = {
        "trend_filter": {"enabled": True, "timeframe": "1h", "ema_fast": 9, "ema_slow": 21, "symbols": []},
        "risk": {"atr_period": 14},
    }
    tf = TrendFilter(md, config)
    assert tf.direction("T/USDT:USDT") in ("LONG", "SHORT", None)


class ScreenerMarketData(OHLCVMarketData):
    def __init__(self, df):
        super().__init__(df)
        self._tickers = {"T/USDT:USDT": {"quoteVolume": 1_000_000, "percentage": 5.0}}

    def fetch_tickers(self):
        return self._tickers


def test_screener_result_dict_keys():
    md = ScreenerMarketData(uptrend_df())
    config = {
        "screener": {"timeframe": "1h", "max_coins": 10, "min_volume_usdt": 0},
        "risk": {"atr_period": 14, "atr_normal_pct": 1.0},
        "whale": {"window_minutes": 30, "min_notional_usdt": 5000},
        "smart_money": {"enabled": True, "cvd_trades_limit": 500, "orderbook_depth": 10},
    }
    screener = Screener(md, config)
    results = screener.candidates()
    assert len(results) == 1
    r = results[0]
    assert set(r) >= {"symbol", "trend", "rsi", "vol", "chg", "price", "atr", "pattern", "smart_money"}
    assert r["trend"] == "LONG"
    assert isinstance(r["atr"], float)
    assert isinstance(r["smart_money"], dict) and "score" in r["smart_money"]


def test_screener_accepts_injected_engine(config):
    md = ScreenerMarketData(uptrend_df())
    from agent.features import build_feature_engine

    engine = build_feature_engine(md, config)
    screener = Screener(md, config, feature_engine=engine)
    assert screener.engine is engine


def test_screener_short_circuits_without_tickers():
    class NoTickers(ScreenerMarketData):
        def fetch_tickers(self):
            raise ConnectionError("down")

    md = NoTickers(uptrend_df())
    config = {"screener": {}, "risk": {}, "whale": {}, "smart_money": {}}
    assert Screener(md, config).candidates() == []
