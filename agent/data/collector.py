import logging
import time

from agent.data.storage import CandleStore

logger = logging.getLogger("trading-agent")


class DataCollector:
    def __init__(self, exchange, store=None, retention_hours=168):
        self.exchange = exchange
        self.store = store or CandleStore(backend="memory", retention_hours=retention_hours)

    def collect(self, symbol, timeframe, limit=200):
        candles = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        self.store.update(symbol, timeframe, candles)
        return self.store.get(symbol, timeframe, limit=limit)

    def collect_many(self, symbols, timeframe, limit=200):
        for symbol in symbols:
            try:
                self.collect(symbol, timeframe, limit)
            except Exception as e:
                logger.warning("collect %s failed: %s", symbol, e)

    def run_loop(self, symbols, timeframe, interval_seconds=60, limit=200):
        while True:
            self.collect_many(symbols, timeframe, limit)
            time.sleep(interval_seconds)
