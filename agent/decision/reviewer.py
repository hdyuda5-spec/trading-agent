"""Trade Reviewer + Missed-Trade Journal (Phase 25).

Two complementary pieces of self-feedback:

* ``TradeReviewer`` classifies closed trades (WIN/LOSS/BREAKEVEN), computes
  MAE/MFE against the ticket's entry and stores a self-feedback journal entry.
* ``MissedTradeJournal`` records every decision we chose not to act on, so the
  cost of being cautious is measurable (preventing the "AFTER_CONSIDERATION"
  habit of second-guessing).
"""

import time
from typing import Any, Dict, List, Optional

from agent.core.utils import is_valid_atr


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class TradeReviewer:
    def __init__(self, store=None, threshold_pct: float = 0.05):
        self.store = store
        self.threshold_pct = float(threshold_pct)  # breakeven band in %

    def classify(self, pnl: float, pnl_pct: Optional[float] = None,
                 reason: Optional[str] = None, sl_hit: bool = False,
                 tp_hit: bool = False) -> str:
        pnl = _f(pnl, 0.0)
        label = "BREAKEVEN" if abs(pnl) < self.threshold_pct else ("WIN" if pnl > 0 else "LOSS")
        if reason:
            base = {"sl": "SL_HIT", "stop_loss": "SL_HIT", "tp": "TP_HIT", "take_profit": "TP_HIT",
                    "cancelled": "CANCELLED", "liquidated": "LIQUIDATED"}.get(str(reason).lower())
            if base:
                label = base
        return label

    def mae_mfe(self, entry: float, high: float, low: float, side: str,
                sl: Optional[float] = None) -> Dict[str, float]:
        """Maximum Adverse/Favorable Excursion in price units (and %)."""
        entry = _f(entry, 0.0)
        high = _f(high, entry)
        low = _f(low, entry)
        if side == "SHORT":
            adverse = max(high - entry, 0.0)
            favorable = max(entry - low, 0.0)
        else:
            adverse = max(entry - low, 0.0)
            favorable = max(high - entry, 0.0)
        base = round(entry, 10) or 1.0
        return {
            "mae": round(adverse, 10),
            "mfe": round(favorable, 10),
            "mae_pct": round(adverse / base * 100.0, 4),
            "mfe_pct": round(favorable / base * 100.0, 4),
        }

    def journal(self, closed: dict) -> Dict[str, Any]:
        """Build a self-feedback journal entry from a closed-trade row."""
        symbol = closed.get("symbol") or closed.get("symbol")
        side = closed.get("side") or closed.get("direction")
        entry = _f(closed.get("entry"))
        pnl = _f(closed.get("pnl"), _f(closed.get("realized_pnl"), 0.0))
        pnl_pct = _f(closed.get("pnl_pct"))
        closed_at = _f(closed.get("closed_at"), _f(closed.get("exit_time")))
        reason = closed.get("reason") or closed.get("exit_reason")
        swing_high = _f(closed.get("swing_high"), _f(closed.get("high")))
        swing_low = _f(closed.get("swing_low"), _f(closed.get("low")))

        base = {
            "symbol": symbol,
            "side": str(side).upper() if side else None,
            "entry": entry,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "closed_at": closed_at or time.time(),
            "reason": reason,
            "label": self.classify(pnl, pnl_pct, reason),
            "ticket_id": closed.get("ticket_id"),
            "misc": {},
        }
        if is_valid_atr(entry) and swing_high is not None and swing_low is not None:
            base["misc"] = self.mae_mfe(entry, swing_high, swing_low, base.get("side") or "LONG")
        return base

    def record_journal(self, closed: dict, store=None) -> Optional[int]:
        store = store or self.store
        if store is None:
            return None
        return store.save_trade_journal(self.journal(closed))


class MissedTradeJournal:
    def __init__(self, store=None):
        self.store = store

    def record(self, symbol: str, side: str, strategy: str, reason_code: str,
               reason: str, price: Optional[float] = None,
               score: Optional[float] = None,
               evidence: Optional[List[dict]] = None,
               metadata: Optional[dict] = None) -> Optional[int]:
        if self.store is None:
            return None
        entry = {
            "symbol": symbol,
            "side": side,
            "strategy": strategy,
            "reason_code": reason_code,
            "reason": reason,
            "price": price,
            "score": score,
            "evidence": evidence or [],
            "metadata": metadata or {},
            "ts": time.time(),
        }
        return self.store.save_missed_trade(entry)

    def summary(self) -> Dict[str, Any]:
        if self.store is None:
            return {}
        return self.store.missed_trades_summary()