import time

from agent.core.support_resistance import SupportResistance
from agent.core.utils import compute_atr, compute_ema, compute_rsi
from agent.strategies.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def __init__(self, config, strat_cfg, exchange, notifier):
        super().__init__(config, strat_cfg, exchange, notifier)
        self.cooldown_seconds = self.strat_cfg.get("cooldown_seconds", 900)
        self._last_signal = {}
        sr_cfg = self.strat_cfg.get("sr_filter", {}) or {}
        self.sr_enabled = sr_cfg.get("enabled", False)
        self.sr = SupportResistance(
            window=sr_cfg.get("window", 3),
            cluster_pct=sr_cfg.get("cluster_pct", 0.5),
        )
        self.sr_zone_pct = float(sr_cfg.get("zone_pct", 0.6))
        self.sr_zone_atr_mult = float(sr_cfg.get("zone_atr_mult", 0))
        self.atr_period = int(self.config.get("risk", {}).get("atr_period", 14))
        self.sr_directions = {
            d: bool(sr_cfg.get("directions", {}).get(d, True)) for d in ("LONG", "SHORT")
        }
        self.sr_symbols = sr_cfg.get("symbols", {}) or {}

    def _sr_symbol_cfg(self, symbol):
        sym_cfg = self.sr_symbols.get(symbol) or {}
        directions = dict(self.sr_directions)
        for d in ("LONG", "SHORT"):
            if d in sym_cfg.get("directions", {}):
                directions[d] = bool(sym_cfg["directions"][d])
        return {
            "enabled": sym_cfg.get("enabled", self.sr_enabled),
            "directions": directions,
            "zone_pct": float(sym_cfg.get("zone_pct", self.sr_zone_pct)),
            "zone_atr_mult": float(sym_cfg.get("zone_atr_mult", self.sr_zone_atr_mult)),
        }

    def _sr_zone(self, df, cfg):
        if cfg["zone_atr_mult"] > 0 and len(df) > 2:
            atr = float(compute_atr(df, self.atr_period).iloc[-1])
            price = float(df["close"].iloc[-1])
            if atr > 0 and price > 0:
                return max(cfg["zone_pct"], cfg["zone_atr_mult"] * atr / price * 100)
        return cfg["zone_pct"]

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

        sr_info = None
        if self.sr_enabled:
            cfg = self._sr_symbol_cfg(symbol)
            if cfg["enabled"] and cfg["directions"].get(signal):
                levels = self.sr.levels(df)
                ok, reason = self.sr.filter(signal, float(close.iloc[-1]), levels, self._sr_zone(df, cfg))
                if not ok:
                    return None
                sr_info = {
                    "reason": reason,
                    "support": levels["support"][-1] if levels["support"] else None,
                    "resistance": levels["resistance"][0] if levels["resistance"] else None,
                }
                if reason.startswith("dekat support") or reason.startswith("dekat resistance"):
                    confidence += 10.0

        metadata = {
            "ema_fast": round(float(cur_fast), 8),
            "ema_slow": round(float(cur_slow), 8),
            "rsi": round(float(cur_rsi), 2),
        }
        if sr_info:
            metadata["sr"] = sr_info

        return {
            "strategy": self.name,
            "symbol": symbol,
            "side": signal,
            "confidence": min(confidence, 100.0),
            "price": float(close.iloc[-1]),
            "metadata": metadata,
        }
