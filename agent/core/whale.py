import time


def should_execute_trade(signal, whale_data, min_whale_txns=3):
    """
    signal: 'BUY' atau 'SELL' dari strategi EMA+RSI
    whale_data: dict berisi net_flow_usdt dan jumlah_transaksi untuk pair ini
    """
    net_flow = whale_data['net_flow_usdt']
    txn_count = whale_data['transaction_count']

    # Kalau data whale terlalu sedikit, jangan jadi penentu (biarkan sinyal lolos)
    if txn_count < min_whale_txns:
        return True, "whale data tipis, filter dilewati"

    if signal == 'BUY':
        if net_flow < 0:
            return False, f"skip BUY: whale net sell {net_flow} USDT"
        return True, f"BUY dikonfirmasi whale net buy {net_flow} USDT"

    if signal == 'SELL':
        if net_flow > 0:
            return False, f"skip SELL: whale net buy {net_flow} USDT"
        return True, f"SELL dikonfirmasi whale net sell {net_flow} USDT"

    return True, "no filter applied"


class WhaleDetector:
    def __init__(self, exchange, config, notifier):
        cfg = config.get("whale", {})
        self.enabled = cfg.get("enabled", True)
        self.min_notional = float(cfg.get("min_notional_usdt", 50000))
        self.max_coins = int(cfg.get("max_coins", 15))
        self.window_seconds = int(cfg.get("window_minutes", 30)) * 60
        self.exchange = exchange
        self.notifier = notifier
        self.last_scan = {}
        self._data_cache = {}

    def scan(self, symbols):
        if not self.enabled:
            return []
        events = []
        self.last_scan = {}
        for symbol in symbols[: self.max_coins]:
            try:
                ev = self._scan_symbol(symbol)
            except Exception:
                ev = None
            if ev:
                events.append(ev)
                self.last_scan[symbol] = ev
        return events

    def data(self, symbol, ttl=300):
        """Scan on-demand per simbol dengan cache TTL. Data segar untuk filter trade."""
        cached = self._data_cache.get(symbol)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]
        try:
            ev = self._scan_symbol(symbol)
        except Exception:
            ev = None
        self._data_cache[symbol] = (time.time(), ev)
        if ev:
            self.last_scan[symbol] = ev
        return ev

    def _scan_symbol(self, symbol):
        trades = self.exchange.fetch_trades(symbol, limit=1000)
        cutoff = (time.time() - self.window_seconds) * 1000
        whales = []
        buy = 0.0
        sell = 0.0
        for t in trades:
            ts = float(t.get("timestamp") or 0)
            if ts and ts < cutoff:
                continue
            amount = float(t.get("amount") or 0)
            price = float(t.get("price") or 0)
            side = t.get("side")
            if side is None:
                is_maker = (t.get("info") or {}).get("m")
                side = "sell" if is_maker in (True, "true", 1, "1") else "buy"
            notional = amount * price
            if notional >= self.min_notional:
                whales.append((side, notional, price, ts))
            if side == "buy":
                buy += notional
            else:
                sell += notional
        if not whales:
            return None
        return {
            "symbol": symbol,
            "n": len(whales),
            "buy_usdt": round(buy, 2),
            "sell_usdt": round(sell, 2),
            "net_usdt": round(buy - sell, 2),
            "direction": "LONG" if buy >= sell else "SHORT",
            "top": [
                {"side": s, "usdt": round(n, 2), "price": p}
                for s, n, p, _ in sorted(whales, key=lambda w: -w[1])[:3]
            ],
        }

    def format(self, events):
        if not events:
            return "🐋 Tidak ada aktivitas whale dalam window"
        lines = [f"🐋 Whale Detector • {len(events)} simbol aktif"]
        for e in events:
            icon = "🟢" if e["direction"] == "LONG" else "🔴"
            sym = e["symbol"].replace("/USDT:USDT", "/USDT")
            lines.append(
                f"{icon} {sym} {e['n']}x whale {e['net_usdt']:+,.0f} USDT "
                f"(buy {e['buy_usdt']:,.0f} / sell {e['sell_usdt']:,.0f})"
            )
        return "\n".join(lines)
