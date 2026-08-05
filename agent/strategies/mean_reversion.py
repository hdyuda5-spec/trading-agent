import time

from agent.core.utils import compute_rsi
from agent.strategies.base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"

    def __init__(self, config, strat_cfg, exchange, notifier):
        super().__init__(config, strat_cfg, exchange, notifier)
        self.bb_period = int(self.strat_cfg.get("bb_period", 20))
        self.bb_std = float(self.strat_cfg.get("bb_std", 2))
        self.rsi_period = int(self.strat_cfg.get("rsi_period", 14))
        self.rsi_oversold = float(self.strat_cfg.get("rsi_oversold", 30))
        self.rsi_overbought = float(self.strat_cfg.get("rsi_overbought", 70))
        self.cooldown_seconds = int(self.strat_cfg.get("cooldown_seconds", 900))
        self._last_signal = {}

    @staticmethod
    def bollinger(series, period=20, std=2):
        sma = series.rolling(period).mean()
        sd = series.rolling(period).std()
        return sma, sma + std * sd, sma - std * sd

    def generate_signal(self, symbol, df):
        close = df["close"]
        sma, upper, lower = self.bollinger(close, self.bb_period, self.bb_std)
        rsi = compute_rsi(close, self.rsi_period)

        if len(df) < max(self.bb_period, self.rsi_period) + 1:
            return None

        cur_close = float(close.iloc[-1])
        cur_rsi = float(rsi.iloc[-1])
        cur_sma = float(sma.iloc[-1])
        cur_upper = float(upper.iloc[-1])
        cur_lower = float(lower.iloc[-1])

        signal = None
        confidence = 60.0

        if cur_close <= cur_lower and cur_rsi < self.rsi_oversold:
            signal = "LONG"
            if cur_rsi < self.rsi_oversold - 10:
                confidence += 20.0
        elif cur_close >= cur_upper and cur_rsi > self.rsi_overbought:
            signal = "SHORT"
            if cur_rsi > self.rsi_overbought + 10:
                confidence += 20.0

        if signal is None:
            return None

        now = time.time()
        last = self._last_signal.get(symbol)
        if last and now - last < self.cooldown_seconds:
            return None
        self._last_signal[symbol] = now

        return {
            "strategy": self.name,
            "symbol": symbol,
            "side": signal,
            "confidence": min(confidence, 100.0),
            "price": cur_close,
            "metadata": {
                "bb_period": self.bb_period,
                "bb_std": self.bb_std,
                "sma": round(float(cur_sma), 8),
                "upper": round(float(cur_upper), 8),
                "lower": round(float(cur_lower), 8),
                "rsi": round(float(cur_rsi), 2),
            },
        }
