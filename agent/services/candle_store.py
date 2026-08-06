"""Candle store — keeps the OHLCV DataFrames that features/strategies consume.

Seeded from REST on startup, then kept fresh event-driven: a ``market.candle``
event either updates the still-forming candle or appends a new closed one.
This is the only module allowed to mutate the candle cache; strategies and the
orchestrator read snapshots through it.
"""

import logging
import threading

import pandas as pd

from agent.core.utils import ohlcv_to_dataframe

logger = logging.getLogger("trading-agent")


class CandleStore:
    def __init__(self):
        self._dfs: dict = {}
        self._lock = threading.RLock()

    def seed(self, symbol: str, ohlcv) -> None:
        """Set the full history for a symbol (REST bootstrap)."""
        df = ohlcv_to_dataframe(ohlcv) if not isinstance(ohlcv, pd.DataFrame) else ohlcv
        with self._lock:
            self._dfs[symbol] = df

    def get(self, symbol: str):
        with self._lock:
            return self._dfs.get(symbol)

    def all(self) -> dict:
        with self._lock:
            return dict(self._dfs)

    def has(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._dfs

    def clear(self) -> None:
        with self._lock:
            self._dfs.clear()

    def update(self, symbol: str, timeframe: str, candle: dict) -> None:
        """Apply a ws/kline event. ``candle``: {ts,o,h,l,c,v,closed} in ms."""
        ts = int(candle.get("ts") or 0)
        if not ts:
            return
        with self._lock:
            df = self._dfs.get(symbol)
            if df is None or df.empty:
                self._dfs[symbol] = ohlcv_to_dataframe(
                    [[ts, candle["o"], candle["h"], candle["l"], candle["c"], candle["v"]]]
                )
                return
            row = pd.to_datetime(ts, unit="ms")
            last = df.index[-1]
            if row <= last:
                df.loc[row, ["open", "high", "low", "close", "volume"]] = [
                    candle["o"], candle["h"], candle["l"], candle["c"], candle["v"],
                ]
            else:
                frame = ohlcv_to_dataframe(
                    [[ts, candle["o"], candle["h"], candle["l"], candle["c"], candle["v"]]]
                )
                self._dfs[symbol] = pd.concat([df, frame])

    def apply_candle_event(self, event) -> None:
        data = event.data
        self.update(data.get("symbol"), data.get("timeframe") or "", data.get("candle") or {})
