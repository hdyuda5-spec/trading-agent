"""Momentum strategy — consumes Feature Engine outputs only.

No indicator is computed here: the EMA cross event, RSI and S/R proximity all
come from feature metadata. The strategy triggers on a fresh fast/slow EMA
cross (Trend feature), filters via RSI (Trend) and S/R zones (Structure), and
the decision pipeline (Trend -> Liquidity -> Structure -> Order Block -> FVG)
aggregates weighted confidence before the Risk Engine attaches SL/TP.
"""

import time
from typing import Any, Optional

from agent.strategies.base import BaseStrategy
from agent.strategies.decision import (
    DecisionEngine,
    RiskEngine,
    build_decision,
)

_SUPPORTED_PAIRS = ((9, 21), (21, 50))


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def __init__(self, config, strat_cfg, exchange, notifier, feature_engine=None):
        super().__init__(config, strat_cfg, exchange, notifier, feature_engine=feature_engine)
        self.ema_fast = int(self.strat_cfg.get("ema_fast", 9))
        self.ema_slow = int(self.strat_cfg.get("ema_slow", 21))
        self.rsi_period = int(self.strat_cfg.get("rsi_period", 14))
        self.rsi_oversold = float(self.strat_cfg.get("rsi_oversold", 30))
        self.rsi_overbought = float(self.strat_cfg.get("rsi_overbought", 70))
        self.require_rsi = bool(self.strat_cfg.get("require_rsi_filter", True))
        self.cooldown_seconds = int(self.strat_cfg.get("cooldown_seconds", 900))
        self.oppose_limit = float(self.strat_cfg.get("oppose_limit", -0.5))
        self._last_signal = {}

        sr_cfg = self.strat_cfg.get("sr_filter", {}) or {}
        self.sr_enabled = bool(sr_cfg.get("enabled", False))
        self.sr_zone_pct = float(sr_cfg.get("zone_pct", 0.6))
        self.sr_directions = {
            d: bool(sr_cfg.get("directions", {}).get(d, True)) for d in ("LONG", "SHORT")
        }
        self.sr_symbols = sr_cfg.get("symbols", {}) or {}

        weights = dict(self.strat_cfg.get("decision_weights", {}) or {})
        self.decision = DecisionEngine(config, weights=weights, feature_engine=feature_engine)
        self.risk_engine = RiskEngine(config)

    def _sr_symbol_cfg(self, symbol):
        sym_cfg = self.sr_symbols.get(symbol) or {}
        directions = dict(self.sr_directions)
        for d in ("LONG", "SHORT"):
            if d in sym_cfg.get("directions", {}):
                directions[d] = bool(sym_cfg["directions"][d])
        return {"enabled": sym_cfg.get("enabled", self.sr_enabled), "directions": directions}

    @staticmethod
    def _ema_pair(meta, fast_p, slow_p):
        """Read the configured EMA pair from Trend feature metadata."""
        if fast_p in (9, 21, 50) and slow_p in (9, 21, 50):
            fast_val = meta.get(f"ema{fast_p}")
            slow_val = meta.get(f"ema{slow_p}")
            if fast_val is not None and slow_val is not None:
                return float(fast_val), float(slow_val)
        return float(meta.get("ema_fast") or 0.0), float(meta.get("ema_slow") or 0.0)

    def _cross_event(self, features) -> Optional[str]:
        """Fresh fast/slow EMA cross from Trend feature metadata."""
        meta = features.trend.metadata
        pair = (self.ema_fast, self.ema_slow)
        if pair in _SUPPORTED_PAIRS:
            cross = meta.get(f"cross_{self.ema_fast}_{self.ema_slow}_event")
        else:
            cross = meta.get(f"cross_{self.ema_fast}_{self.ema_slow}_event") or meta.get(
                f"cross_{self.ema_fast}_{self.ema_slow}"
            )
        if cross in ("bullish", "bearish"):
            return cross
        return None

    def _rsi_ok(self, side, rsi):
        if side == "LONG":
            return rsi < self.rsi_overbought
        return rsi > self.rsi_oversold

    def _rsi_bonus(self, side, rsi):
        if side == "LONG":
            if rsi < self.rsi_oversold:
                return 0.15
            if rsi > 50:
                return 0.05
        else:
            if rsi > self.rsi_overbought:
                return 0.15
            if rsi < 50:
                return 0.05
        return 0.0

    def _sr_filter(self, side, features):
        """S/R proximity check from Structure feature metadata (lists + price)."""
        meta = features.structure.metadata
        price = float(meta.get("price") or 0.0)
        support = sorted([float(s) for s in meta.get("support", []) if float(s) < price])
        resistance = sorted([float(r) for r in meta.get("resistance", []) if float(r) > price])
        zone = self.sr_zone_pct / 100.0
        if side == "LONG":
            if resistance and (resistance[0] - price) / price <= zone:
                return False, f"harga {price:.4g} dekat resistance {resistance[0]:.4g}", None
            if support and (price - support[-1]) / price <= zone:
                return True, f"LONG dekat support {support[-1]:.4g}", {
                    "reason": f"dekat support {support[-1]:.4g}",
                    "support": support[-1],
                    "resistance": resistance[0] if resistance else None,
                }
            return True, "S/R netral", None
        if support and (price - support[-1]) / price <= zone:
            return False, f"harga {price:.4g} dekat support {support[-1]:.4g}", None
        if resistance and (resistance[0] - price) / price <= zone:
            return True, f"SHORT dekat resistance {resistance[0]:.4g}", {
                "reason": f"dekat resistance {resistance[0]:.4g}",
                "support": support[-1] if support else None,
                "resistance": resistance[0],
            }
        return True, "S/R netral", None

    def generate_signal(self, symbol, df, features=None):
        verdict = self.decision.decide(symbol, df, features)
        if verdict is None:
            return None
        fs = verdict["features"]

        if len(df) < max(self.ema_slow, self.rsi_period) + 2:
            return None

        cross = self._cross_event(fs)
        if cross is None:
            return None
        gate_side = "LONG" if cross == "bullish" else "SHORT"
        side_sign = 1 if gate_side == "LONG" else -1

        if verdict["margin"] * side_sign < self.oppose_limit:
            return None

        rsi = fs.trend.metadata.get("rsi")
        if self.require_rsi:
            if rsi is None:
                return None
            if not self._rsi_ok(gate_side, rsi):
                return None

        now = time.time()
        last = self._last_signal.get(symbol)
        if last and now - last < self.cooldown_seconds:
            return None
        self._last_signal[symbol] = now

        sr_info = None
        if self.sr_enabled:
            cfg = self._sr_symbol_cfg(symbol)
            if cfg["enabled"] and cfg["directions"].get(gate_side):
                ok, _, sr_info = self._sr_filter(gate_side, fs)
                if not ok:
                    return None

        price = verdict["price"]
        risk = self.risk_engine.compute(price, gate_side, verdict["atr"])
        if risk is None:
            return None

        confidence = min(0.95, 0.55 + 0.5 * max(0.0, verdict["margin"] * side_sign))
        if rsi is not None:
            confidence = min(0.95, confidence + self._rsi_bonus(gate_side, rsi))
        reasons = list(verdict["reasons"])
        reasons.append(
            f"momentum: cross EMA {self.ema_fast}/{self.ema_slow} {cross}"
            f" (rsi {rsi:.1f})" if rsi is not None else f"momentum: cross EMA {self.ema_fast}/{self.ema_slow} {cross}"
        )

        fast_cur, slow_cur = self._ema_pair(fs.trend.metadata, self.ema_fast, self.ema_slow)
        metadata = {
            "ema_fast": round(fast_cur, 8),
            "ema_slow": round(slow_cur, 8),
            "ema_fast_period": self.ema_fast,
            "ema_slow_period": self.ema_slow,
            "rsi": round(rsi, 2) if rsi is not None else None,
            "cross": cross,
            "margin": verdict["margin"],
            "alignment": round(max(0.0, verdict["margin"] * (1 if gate_side == "LONG" else -1)), 4),
        }
        if sr_info:
            metadata["sr"] = sr_info

        return build_decision(self.name, symbol, gate_side, confidence, reasons, risk, price, metadata)
