import time

import numpy as np
import pandas as pd
import pytest


def make_df(close, volume=None, high_offset=1.0, low_offset=1.0, start_ms=1700000000000, step_ms=900000):
    close = np.asarray(close, dtype=float)
    n = len(close)
    volume = np.full(n, 100.0) if volume is None else np.asarray(volume, dtype=float)
    df = pd.DataFrame(
        {
            "open": close - 0.3,
            "high": close + high_offset,
            "low": close - low_offset,
            "close": close,
            "volume": volume,
        }
    )
    df.index = pd.to_datetime([start_ms + i * step_ms for i in range(n)], unit="ms")
    return df


def uptrend_df(n=120):
    """Monotonic uptrend — should read as bullish across trend/volume features."""
    close = np.linspace(100, 140, n)
    vol = np.linspace(100, 300, n)
    return make_df(close, vol)


def downtrend_df(n=120):
    close = np.linspace(140, 100, n)
    vol = np.linspace(300, 100, n)
    return make_df(close, vol)


def flat_df(n=120):
    """Perfectly flat close — low ATR, no trend, no S/R proximity."""
    close = np.full(n, 100.0)
    return make_df(close, high_offset=0.05, low_offset=0.05)


def choppy_df(n=120):
    """High-range random walk — high ATR relative to price."""
    rng = np.random.RandomState(2)
    close = 100 + np.cumsum(rng.randn(n) * 3.0)
    return make_df(close, high_offset=3.0, low_offset=3.0)


def cross_df():
    """Deterministic close series whose EMA9 crosses above EMA21 on the last
    candle (seed 283), so momentum emits a fresh LONG entry."""
    seed = 283
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.randn(80) * 0.4 + 0.03)
    close[-5:] += np.random.RandomState(seed + 100).rand(5) * 8
    return make_df(close)


def near_resistance_df():
    """Rise to a pivot high, pull back, then zigzag back up to just under it."""
    rise = np.linspace(100, 110, 31)
    fall = np.linspace(110, 104, 21)[1:]
    rec = np.array([104.5, 104.0, 105.5, 105.0, 106.5, 106.0, 107.5, 107.0, 108.5, 108.0, 109.5, 109.0, 110.0, 109.5, 110.8])
    return make_df(np.concatenate([rise, fall, rec]))


@pytest.fixture
def config():
    return {
        "risk": {"atr_period": 14, "atr_normal_pct": 1.0},
        "whale": {"window_minutes": 30, "min_notional_usdt": 5000},
        "smart_money": {"enabled": True, "cvd_trades_limit": 500, "orderbook_depth": 10},
        "features": {},
    }


class FakeMarketData:
    """Deterministic, call-counting MarketData fake."""

    def __init__(self, order_book=None, trades=None, funding_rate=None):
        self.order_book = order_book or {
            "bids": [[100.0, 5.0], [99.9, 5.0]],
            "asks": [[100.1, 5.0], [100.2, 5.0]],
        }
        self.trades = trades if trades is not None else self._default_trades()
        self._funding_rate = funding_rate
        self.calls = {"order_book": 0, "trades": 0, "funding": 0, "ohlcv": 0}

    def _default_trades(self):
        now = int(time.time() * 1000)
        return [
            {"timestamp": now, "amount": 100.0, "price": 100.0, "side": "buy"},
            {"timestamp": now, "amount": 50.0, "price": 100.0, "side": "sell"},
        ]

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=200, since=None):
        self.calls["ohlcv"] += 1
        return []

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def fetch_tickers(self):
        return {}

    def fetch_order_book(self, symbol, limit=5):
        self.calls["order_book"] += 1
        return self.order_book

    def fetch_trades(self, symbol, limit=500):
        self.calls["trades"] += 1
        return self.trades

    def fetch_funding_rate(self, symbol):
        self.calls["funding"] += 1
        return self._funding_rate
