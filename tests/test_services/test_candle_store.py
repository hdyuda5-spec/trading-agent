"""CandleStore: event-driven OHLCV cache semantics."""

import pandas as pd
import pytest

from agent.core.utils import ohlcv_to_dataframe
from agent.services.candle_store import CandleStore
from agent.services.event_bus import Event

T0 = 1700000000000
STEP = 900000


def candle(ts, close, closed=False, o=100.0, h=101.0, l=99.0, v=10.0):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": close, "v": v, "closed": closed}


def ohlcv_rows(*closes):
    return [[T0 + i * STEP, c - 0.5, c + 1.0, c - 1.0, c, 10.0] for i, c in enumerate(closes)]


@pytest.fixture
def store():
    return CandleStore()


class TestSeed:
    def test_seed_ohlcv_list(self, store):
        store.seed("BNB/USDT:USDT", ohlcv_rows(100, 101, 102))
        df = store.get("BNB/USDT:USDT")
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert len(df) == 3
        assert store.has("BNB/USDT:USDT")

    def test_seed_dataframe(self, store):
        df = ohlcv_to_dataframe(ohlcv_rows(100, 101))
        store.seed("BNB/USDT:USDT", df)
        assert store.get("BNB/USDT:USDT") is df

    def test_missing_symbol_returns_none(self, store):
        assert store.get("NOPE") is None
        assert not store.has("NOPE")

    def test_clear_and_all(self, store):
        store.seed("A", ohlcv_rows(1))
        store.seed("B", ohlcv_rows(2))
        assert set(store.all()) == {"A", "B"}
        store.clear()
        assert store.all() == {}


class TestUpdate:
    def test_updates_forming_candle_in_place(self, store):
        store.seed("S", ohlcv_rows(100))
        store.update("S", "15m", candle(T0, close=105.0))
        df = store.get("S")
        assert len(df) == 1
        assert df["close"].iloc[-1] == 105.0

    def test_appends_new_candle(self, store):
        store.seed("S", ohlcv_rows(100))
        store.update("S", "15m", candle(T0 + STEP, close=110.0))
        df = store.get("S")
        assert len(df) == 2
        assert df["close"].iloc[-1] == 110.0

    def test_seeds_when_empty(self, store):
        store.update("S", "15m", candle(T0, close=99.0))
        df = store.get("S")
        assert len(df) == 1
        assert df["close"].iloc[0] == 99.0

    def test_ignores_missing_ts(self, store):
        store.seed("S", ohlcv_rows(100))
        store.update("S", "15m", candle(0, close=105.0))
        assert store.get("S")["close"].iloc[-1] == 100.0

    def test_apply_candle_event(self, store):
        store.update("S", "15m", candle(T0, close=100.0))
        store.apply_candle_event(
            Event(
                "market.candle",
                {"symbol": "S", "timeframe": "15m", "candle": candle(T0 + STEP, close=120.0)},
            )
        )
        assert store.get("S")["close"].iloc[-1] == 120.0
