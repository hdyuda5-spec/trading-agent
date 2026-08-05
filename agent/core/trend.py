import math
import time

from agent.core.utils import CANDLE_TF_SECONDS, compute_adx, ohlcv_to_dataframe
from agent.features.trend import TrendFeature


class TrendFilter:
    def __init__(self, exchange, config):
        cfg = config.get("trend_filter", {})
        self.enabled = cfg.get("enabled", False)
        self.timeframe = cfg.get("timeframe", "1h")
        self.fast = cfg.get("ema_fast", 21)
        self.slow = cfg.get("ema_slow", 50)
        self.symbols = set(cfg.get("symbols", []))
        adx_cfg = cfg.get("adx_filter", {})
        self.adx_enabled = adx_cfg.get("enabled", True)
        self.adx_period = int(adx_cfg.get("period", 14))
        self.adx_min = float(adx_cfg.get("min_adx", 20))
        self.exchange = exchange
        self._cache = {}
        # Trend direction is delegated to the shared TrendFeature so both the
        # filter and the Feature Engine interpret the EMAs identically.
        self._trend_feature = TrendFeature(
            exchange,
            {
                "features": {
                    "trend": {
                        "mode": "ema_cross",
                        "ema_fast": self.fast,
                        "ema_slow": self.slow,
                        "adx_enabled": self.adx_enabled,
                        "adx_period": self.adx_period,
                        "min_adx": self.adx_min,
                    }
                }
            },
        )

    def active_for(self, symbol):
        return self.enabled and (not self.symbols or symbol in self.symbols)

    def _ohlcv(self, symbol):
        """Fetch OHLCV timeframe trend dengan cache TTL. Dipakai bersama
        direction() dan is_trending() supaya tidak fetch dua kali."""
        cached = self._cache.get(symbol)
        ttl = CANDLE_TF_SECONDS.get(self.timeframe, 3600)
        if cached and time.time() - cached["ts"] < ttl:
            return cached["df"]
        try:
            df = ohlcv_to_dataframe(self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=200))
        except Exception:
            return None
        self._cache[symbol] = {"df": df, "ts": time.time()}
        return df

    def direction(self, symbol):
        if not self.active_for(symbol):
            return None
        df = self._ohlcv(symbol)
        if df is None or len(df) < self.slow + 2:
            return None
        result = self._trend_feature.compute(symbol, df)
        return {"bullish": "LONG", "bearish": "SHORT"}.get(result.signal)

    def last_adx(self, symbol):
        df = self._ohlcv(symbol)
        if df is None or len(df) < self.adx_period * 2:
            return None
        try:
            adx = float(compute_adx(df, self.adx_period).iloc[-1])
        except Exception:
            return None
        if math.isnan(adx):
            return None
        return adx

    def is_trending(self, symbol):
        """True = trending (ADX >= min_adx). False = ranging/choppy -> skip sinyal.
        Fail-open: kalau ADX tak bisa dihitung, default True supaya tidak memblokir."""
        if not self.enabled or not self.adx_enabled:
            return True
        adx = self.last_adx(symbol)
        if adx is None:
            return True
        return adx >= self.adx_min
