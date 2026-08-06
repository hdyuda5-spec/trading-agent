"""Whale service — periodic whale scans published as events + notifications."""

import logging
import threading
import time

from agent.services.event_bus import Event

logger = logging.getLogger("trading-agent")


class WhaleService:
    def __init__(self, config, exchange, notifier, whale, bus):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.whale = whale
        self.bus = bus
        self._last_scan = 0.0
        self._lock = threading.RLock()

    def should_scan(self, now=None) -> bool:
        wcfg = self.config.get("whale", {})
        if not wcfg.get("enabled", True):
            return False
        interval = int(wcfg.get("interval_minutes", 10)) * 60
        if interval <= 0:
            return False
        return time.time() - self._last_scan >= interval

    def mark_scan(self) -> None:
        self._last_scan = time.time()

    def run(self) -> list:
        self.mark_scan()
        try:
            events = self.whale.scan(self._symbols())
            if not events:
                return []
            min_net = float(self.config.get("whale", {}).get("min_alert_net_usdt", 0))
            if min_net > 0 and max(abs(e["net_usdt"]) for e in events) < min_net:
                return []
            self.notifier.send(self.whale.format(events))
            self.bus.publish_sync(Event("whale.ready", {"events": events}, source="whale"))
            return events
        except Exception as e:
            self.notifier.alert("Whale scan gagal", str(e))
            return []

    def _symbols(self):
        symbols = list(self.config["symbols"])
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            tickers = {}
        rows = [
            (s, float(t.get("quoteVolume") or 0))
            for s, t in tickers.items()
            if s.endswith("/USDT:USDT")
        ]
        rows.sort(key=lambda r: -r[1])
        for s, _ in rows[: int(self.config.get("whale", {}).get("max_coins", 15))]:
            if s not in symbols:
                symbols.append(s)
        return symbols
