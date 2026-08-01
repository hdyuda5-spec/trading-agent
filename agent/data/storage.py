import os
import sqlite3
import threading
from datetime import datetime, timezone

import pandas as pd

from agent.core.utils import ohlcv_to_dataframe


class CandleStore:
    def __init__(self, backend="memory", path="data/candles.db", retention_hours=168):
        self._lock = threading.RLock()
        self.backend = backend
        self._memory = {}
        self._latest = {}
        self.retention_hours = retention_hours
        self._db = None
        if backend == "sqlite":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._init_db()
            self._load_db()

    def _init_db(self):
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                ts INTEGER NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, timeframe, ts)
            )
            """
        )
        self._db.commit()

    def _load_db(self):
        cur = self._db.execute("SELECT DISTINCT symbol, timeframe FROM candles")
        for symbol, tf in cur.fetchall():
            df = pd.read_sql_query(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE symbol=? AND timeframe=? ORDER BY ts",
                self._db,
                params=(symbol, tf),
            )
            if df.empty:
                continue
            df["timestamp"] = pd.to_datetime(df["ts"], unit="ms")
            df = df.set_index("timestamp")
            self._memory[(symbol, tf)] = df
            self._latest[symbol] = {"price": float(df["close"].iloc[-1]), "ts": int(df["ts"].iloc[-1])}

    def update(self, symbol, timeframe, candles):
        if not candles:
            return
        df = ohlcv_to_dataframe(candles)
        with self._lock:
            key = (symbol, timeframe)
            existing = self._memory.get(key)
            if existing is not None and not existing.empty:
                df = pd.concat([existing, df])
            df = df[~df.index.duplicated(keep="last")].sort_index()
            df = self._apply_retention(df)
            self._memory[key] = df
            if self._db is not None:
                self._persist(key, df)
            self._latest[symbol] = {
                "price": float(df["close"].iloc[-1]),
                "ts": int(df.index[-1].value // 1_000_000),
            }

    def _apply_retention(self, df):
        if not self.retention_hours:
            return df
        cutoff = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None) - pd.Timedelta(hours=self.retention_hours)
        return df[df.index >= cutoff]

    def _persist(self, key, df):
        symbol, tf = key
        rows = [
            (symbol, tf, int(ts.value // 1_000_000), r.open, r.high, r.low, r.close, r.volume)
            for ts, r in df.iterrows()
        ]
        self._db.executemany(
            "INSERT OR REPLACE INTO candles (symbol, timeframe, ts, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        self._db.commit()

    def get(self, symbol, timeframe, limit=200):
        with self._lock:
            df = self._memory.get((symbol, timeframe))
            if df is None or df.empty:
                return None
            return df.tail(limit).copy()

    def latest_price(self, symbol):
        with self._lock:
            hit = self._latest.get(symbol)
            return hit["price"] if hit else None

    def info(self):
        with self._lock:
            return {
                "keys": len(self._memory),
                "latest": {k: v for k, v in self._latest.items()},
            }

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
