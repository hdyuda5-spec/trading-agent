import time

from agent.core.utils import CANDLE_TF_SECONDS, compute_ema, ohlcv_to_dataframe


class TrendFilter:
    def __init__(self, exchange, config):
        cfg = config.get("trend_filter", {})
        self.enabled = cfg.get("enabled", False)
        self.timeframe = cfg.get("timeframe", "1h")
        self.fast = cfg.get("ema_fast", 21)
        self.slow = cfg.get("ema_slow", 50)
        self.symbols = set(cfg.get("symbols", []))
        self.exchange = exchange
        self._cache = {}

    def active_for(self, symbol):
        return self.enabled and (not self.symbols or symbol in self.symbols)

    def direction(self, symbol):
        if not self.active_for(symbol):
            return None
        cached = self._cache.get(symbol)
        ttl = CANDLE_TF_SECONDS.get(self.timeframe, 3600)
        if cached and time.time() - cached["ts"] < ttl:
            return cached["dir"]
        try:
            df = ohlcv_to_dataframe(self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=200))
            if len(df) < self.slow + 2:
                return None
            fast = compute_ema(df["close"], self.fast).iloc[-1]
            slow = compute_ema(df["close"], self.slow).iloc[-1]
            direction = "LONG" if fast > slow else ("SHORT" if fast < slow else None)
            self._cache[symbol] = {"dir": direction, "ts": time.time()}
            return direction
        except Exception:
            return None
