import time

from agent.core.support_resistance import SupportResistance
from agent.core.utils import compute_ema
from agent.strategies.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def __init__(self, config, strat_cfg, exchange, notifier, feature_engine=None):
        super().__init__(config, strat_cfg, exchange, notifier, feature_engine=feature_engine)
        self.cooldown_seconds = self.strat_cfg.get("cooldown_seconds", 900)
        self._last_signal = {}
        sr_cfg = self.strat_cfg.get("sr_filter", {}) or {}
        self.sr_enabled = sr_cfg.get("enabled", False)
        self.sr = SupportResistance(
            window=sr_cfg.get("window", 3),
            cluster_pct=sr_cfg.get("cluster_pct", 0.5),
        )
        self.sr_window = int(sr_cfg.get("window", 3))
        self.sr_cluster_pct = float(sr_cfg.get("cluster_pct", 0.5))
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

    def _sr_zone(self, cfg, atr, price):
        if cfg["zone_atr_mult"] > 0 and atr and price:
            return max(cfg["zone_pct"], cfg["zone_atr_mult"] * atr / price * 100)
        return cfg["zone_pct"]

    def _cross_from_features(self, features, fast_p, slow_p, close):
        """Read the EMA crossover *event* from TrendFeature metadata when the
        configured periods match the ones the feature precomputes; otherwise
        fall back to a df computation so custom periods keep working."""
        meta = features.trend.metadata
        if (fast_p, slow_p) == (9, 21):
            cross = meta.get("cross_9_21_event")
            fast_cur, slow_cur = meta.get("ema9"), meta.get("ema21")
            return cross, fast_cur, slow_cur
        if (fast_p, slow_p) == (21, 50):
            cross = meta.get("cross_21_50_event")
            fast_cur, slow_cur = meta.get("ema21"), meta.get("ema50")
            return cross, fast_cur, slow_cur
        fast = compute_ema(close, fast_p)
        slow = compute_ema(close, slow_p)
        prev_f, cur_f = float(fast.iloc[-2]), float(fast.iloc[-1])
        prev_s, cur_s = float(slow.iloc[-2]), float(slow.iloc[-1])
        if prev_f <= prev_s and cur_f > cur_s:
            cross = "bullish"
        elif prev_f >= prev_s and cur_f < cur_s:
            cross = "bearish"
        else:
            cross = None
        return cross, cur_f, cur_s

    def _structure_levels(self, features, df):
        """S/R levels from the structure feature when its window/cluster config
        matches this strategy's, otherwise recompute from df (backward compat)."""
        meta = features.structure.metadata
        if meta.get("sr_window") == self.sr_window and meta.get("sr_cluster_pct") == self.sr_cluster_pct:
            return {
                "support": list(meta.get("support", [])),
                "resistance": list(meta.get("resistance", [])),
            }
        return self.sr.levels(df)

    def generate_signal(self, symbol, df, features=None):
        features = self.resolve_features(symbol, df, features)

        ema_fast_p = self.strat_cfg["ema_fast"]
        ema_slow_p = self.strat_cfg["ema_slow"]
        rsi_period = self.strat_cfg["rsi_period"]
        rsi_oversold = self.strat_cfg["rsi_oversold"]
        rsi_overbought = self.strat_cfg["rsi_overbought"]
        require_rsi = self.strat_cfg["require_rsi_filter"]

        if len(df) < max(ema_slow_p, rsi_period) + 2:
            return None

        close = df["close"]
        cross, fast_cur, slow_cur = self._cross_from_features(features, ema_fast_p, ema_slow_p, close)

        if cross is None:
            return None
        signal = "LONG" if cross == "bullish" else "SHORT"
        confidence = 60.0
        cur_rsi = features.trend.metadata.get("rsi")

        if require_rsi:
            if cur_rsi is None:
                return None
            if signal == "LONG":
                if cur_rsi >= rsi_overbought:
                    return None
                if cur_rsi < rsi_oversold:
                    confidence += 25.0
                elif cur_rsi > 50:
                    confidence += 10.0
            else:
                if cur_rsi <= rsi_oversold:
                    return None
                if cur_rsi > rsi_overbought:
                    confidence += 25.0
                elif cur_rsi < 50:
                    confidence += 10.0

        now = time.time()
        last = self._last_signal.get(symbol)
        if last and now - last < self.cooldown_seconds:
            return None
        self._last_signal[symbol] = now

        sr_info = None
        if self.sr_enabled:
            cfg = self._sr_symbol_cfg(symbol)
            if cfg["enabled"] and cfg["directions"].get(signal):
                levels = self._structure_levels(features, df)
                price = float(close.iloc[-1])
                atr = features.volatility.metadata.get("atr")
                ok, reason = self.sr.filter(signal, price, levels, self._sr_zone(cfg, atr, price))
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
            "ema_fast": round(float(fast_cur or 0.0), 8),
            "ema_slow": round(float(slow_cur or 0.0), 8),
            "rsi": round(float(cur_rsi), 2) if cur_rsi is not None else None,
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
