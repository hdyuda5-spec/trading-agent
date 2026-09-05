import time


def aggregate_whale_flow(trades, window_seconds, min_notional, now_ms=None):
    """Aggregate whale transactions from a list of public trades.

    Pure function shared by ``WhaleDetector._scan_symbol`` and the Whale
    feature so both interpret the same data identically.

    Returns ``None`` when no trade meets ``min_notional`` within the window,
    otherwise a dict with ``n``, ``buy_usdt``, ``sell_usdt``, ``net_usdt``,
    ``direction`` and ``top`` (largest whale fills).
    """
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    cutoff = now_ms - window_seconds * 1000
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
        if notional >= min_notional:
            whales.append((side, notional, price, ts))
        if side == "buy":
            buy += notional
        else:
            sell += notional
    if not whales:
        return None
    return {
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


def should_execute_trade(signal, whale_data, min_whale_txns=3, min_net_usdt=0):
    """
    signal: 'BUY' atau 'SELL' dari strategi EMA+RSI
    whale_data: dict berisi net_flow_usdt dan jumlah_transaksi untuk pair ini
    min_net_usdt: ambang signifikan; hanya memblokir kalau net flow berlawanan
                  lebih besar dari ambang ini.
    """
    net_flow = whale_data['net_flow_usdt']
    txn_count = whale_data['transaction_count']

    # Kalau data whale terlalu sedikit, jangan jadi penentu (biarkan sinyal lolos)
    if txn_count < min_whale_txns:
        return True, "whale data tipis, filter dilewati"

    # Net flow kecil/belum signifikan: jangan blokir, hanya info
    if abs(net_flow) < min_net_usdt:
        return True, f"whale net {net_flow} USDT di bawah ambang {min_net_usdt}, tidak memblokir"

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
        self._market_cache = None

    def market_net_flow(self, symbols_provider, ttl=300):
        """Net-flow whale agregat lintas simbol teratas (regime pasar).
        symbols_provider: callable -> list simbol, dievaluasi hanya saat cache expired.
        Return (total_net_usdt, jumlah_simbol_dgn_data)."""
        now = time.time()
        if self._market_cache and now - self._market_cache[0] < ttl:
            return self._market_cache[1], self._market_cache[2]
        total = 0.0
        with_data = 0
        try:
            symbols = symbols_provider()[: self.max_coins]
        except Exception:
            symbols = []
        for symbol in symbols:
            ev = self.data(symbol)
            if ev:
                total += ev["net_usdt"]
                with_data += 1
        self._market_cache = (now, total, with_data)
        return total, with_data

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
        ev = aggregate_whale_flow(trades, self.window_seconds, self.min_notional)
        if ev:
            ev["symbol"] = symbol
        return ev

    def format(self, events):
        from agent.execution.notifier import fmt_usdt

        if not events:
            return "🐋 Tidak ada aktivitas whale dalam window"
        longs = [e for e in events if e["direction"] == "LONG"]
        shorts = [e for e in events if e["direction"] != "LONG"]
        lines = [f"🐋 WHALE · {len(events)} simbol"]
        for grp, icon, label in ((longs, "🟢", "BELI BERSIH"), (shorts, "🔴", "JUAL BERSIH")):
            if not grp:
                continue
            lines.append(f"{icon} {label} ({len(grp)})")
            for e in grp:
                sym = e["symbol"].replace("/USDT:USDT", "/USDT")
                net = f"{'+' if e['net_usdt'] >= 0 else ''}{fmt_usdt(e['net_usdt'])} USDT"
                lines.append(f"  {sym}: {net} ({e['n']}x)")
        return "\n".join(lines)
