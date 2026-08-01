import time

from agent.core.utils import compute_ema, compute_rsi
from agent.strategies.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def __init__(self, config, strat_cfg, exchange, notifier):
        super().__init__(config, strat_cfg, exchange, notifier)
        self.cooldown_seconds = self.strat_cfg.get("cooldown_seconds", 900)
        self._last_signal = {}

    def generate_signal(self, symbol, df):
        ema_fast = self.strat_cfg["ema_fast"]
        ema_slow = self.strat_cfg["ema_slow"]
        rsi_period = self.strat_cfg["rsi_period"]
        rsi_oversold = self.strat_cfg["rsi_oversold"]
        rsi_overbought = self.strat_cfg["rsi_overbought"]
        require_rsi = self.strat_cfg["require_rsi_filter"]

        close = df["close"]
        fast = compute_ema(close, ema_fast)
        slow = compute_ema(close, ema_slow)
        rsi = compute_rsi(close, rsi_period)

        if len(df) < max(ema_slow, rsi_period) + 2:
            return None

        prev_fast, prev_slow = fast.iloc[-2], slow.iloc[-2]
        cur_fast, cur_slow = fast.iloc[-1], slow.iloc[-1]
        cur_rsi = rsi.iloc[-1]

        signal = None
        confidence = 60.0

        if prev_fast <= prev_slow and cur_fast > cur_slow:
            signal = "LONG"
            if require_rsi and cur_rsi >= rsi_overbought:
                return None
            if require_rsi and cur_rsi < rsi_oversold:
                confidence += 25.0
            elif require_rsi and cur_rsi > 50:
                confidence += 10.0
        elif prev_fast >= prev_slow and cur_fast < cur_slow:
            signal = "SHORT"
            if require_rsi and cur_rsi <= rsi_oversold:
                return None
            if require_rsi and cur_rsi > rsi_overbought:
                confidence += 25.0
            elif require_rsi and cur_rsi < 50:
                confidence += 10.0

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
            "price": float(close.iloc[-1]),
            "metadata": {
                "ema_fast": round(float(cur_fast), 8),
                "ema_slow": round(float(cur_slow), 8),
                "rsi": round(float(cur_rsi), 2),
            },
        }
