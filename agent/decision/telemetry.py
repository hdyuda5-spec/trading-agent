"""Signal funnel / rejection telemetry (Phase 8).

Counts every stage from raw signal to live fill so the funnel can be analysed:

    strategy_signals → passed_gate → risk_rejected → tickets_created
      → orders_submitted → fills → cancelled/expired

Counters persist through the TradeStore (``save_funnel``/``load_funnel``).
"""

import json
import time
from collections import Counter
from typing import Any, Dict, Optional


class SignalFunnel:
    KEYS = (
        "strategy_signals",
        "passed_gate",
        "risk_rejected",
        "tickets_created",
        "orders_submitted",
        "fills",
        "cancelled",
        "expired",
        "rejected_total",
        "no_signal",
    )

    def __init__(self, store=None):
        self.store = store
        self._count = Counter()
        self._rejects: Counter = Counter()  # reason_code → n
        self._last_reset = time.time()
        if store is not None:
            try:
                self.load()
            except Exception:
                pass

    # -- counting --------------------------------------------------------

    def inc(self, key: str, n: int = 1) -> None:
        if key in self.KEYS:
            self._count[key] += n

    def reject(self, reason_code: str, key: str = "rejected_total") -> None:
        assert key in self.KEYS
        self._count[key] += 1
        if reason_code:
            self._rejects[reason_code] += 1

    def notify(self, event: str, symbol: str = "N/A", **kwargs) -> None:
        """Alerts / integrations hook. Providers register ``self._listeners``."""
        for fn in getattr(self, "_listeners", []):
            try:
                fn(event, symbol=symbol, **kwargs)
            except Exception:
                pass

    def attach(self, listener) -> None:
        if not hasattr(self, "_listeners"):
            self._listeners = []
        self._listeners = list(getattr(self, "_listeners", [])) + [listener]

    # -- snapshots / persistence ------------------------------------------

    def snapshot(self) -> Dict[str, int]:
        return dict(self._count)

    def rejects(self) -> Dict[str, int]:
        return dict(self._rejects)

    def funnel(self) -> Dict[str, int]:
        data = {k: self._count.get(k, 0) for k in self.KEYS}
        data["rejected"] = dict(self._rejects)
        return data

    def save(self) -> None:
        if self.store is not None:
            self.store.save_funnel(self.funnel())

    def load(self) -> None:
        if self.store is None:
            return
        data = self.store.load_funnel()
        if not data:
            return
        for k, v in data.items():
            if k == "rejected" and isinstance(v, dict):
                self._rejects.update({str(c): int(n) for c, n in v.items()})
            elif k in self.KEYS:
                try:
                    self._count[k] = int(v)
                except (TypeError, ValueError):
                    pass