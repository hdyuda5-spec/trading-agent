import argparse
import json
import logging
import math
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.core.exchange import ExchangeClient
from agent.core.utils import compute_adx, compute_atr, compute_ema, compute_rsi, ohlcv_to_dataframe

# Fill rate limit order dari AUDIT Bagian A (simulasi postOnly best bid/ask, TTL 180 s):
# hanya sinyal dengan kandidat tersebut yang dianggap eksekusi di skenario limit order.
PART_A_FILL_RATES = {
    "BTC/USDT:USDT": 0.476,
    "ETH/USDT:USDT": 0.475,
    "SNDK/USDT:USDT": 0.310,
}
PART_A_FILL_RATE_OVERALL = 0.406

FUNDING_INTERVAL_SEC = 8 * 3600  # funding Binance USDT-M: 00:00/08:00/16:00 UTC


class Backtester:
    def __init__(self, config, symbol, timeframe="15m", strategy="momentum", initial_equity=10000.0,
                 fee=None, order_type="market", slippage_pct=0.05, fill_rate=None, rng_seed=20260805):
        self.cfg = config
        self.symbol = symbol
        self.timeframe = timeframe
        self.strategy = strategy
        self.initial_equity = float(initial_equity)
        self.order_type = order_type
        self.slippage = float(slippage_pct) / 100.0
        if fill_rate is None:
            fill_rate = 1.0 if order_type == "market" else PART_A_FILL_RATES.get(symbol, PART_A_FILL_RATE_OVERALL)
        self.fill_rate = float(fill_rate)
        self.rng = np.random.default_rng(rng_seed)
        # fee: taker 0.05% utk market, maker 0.02% utk limit; --fee override kalau dipakai
        if fee is None:
            fee = 0.0005 if order_type == "market" else 0.0002
        self.fee = float(fee)
        self._funding = []  # list (ts_ms, funding_rate)
        self._funding_bar = None

    # ------------------------------------------------------------------ data
    def load_data(self, days=90, testnet=True, timeframe=None):
        timeframe = timeframe or self.timeframe
        ex = ExchangeClient(name=self.cfg["exchange"]["name"], testnet=testnet,
                            options=self.cfg["exchange"].get("options", {}))
        tf_sec = CANDLE_TF
        total = int((days * 86400) / tf_sec.get(timeframe, 900)) + 10
        batch = 1000
        all_rows = []
        start = int((time.time() - days * 86400) * 1000)
        end = int(time.time() * 1000)
        while start < end and len(all_rows) < total:
            try:
                rows = ex.fetch_ohlcv(self.symbol, timeframe, since=start, limit=batch)
            except Exception:
                break
            if not rows:
                break
            all_rows.extend(rows)
            last_ts = rows[-1][0]
            if last_ts <= start:
                break
            start = last_ts + 1
        ex.client.close()
        if not all_rows:
            raise RuntimeError(f"no OHLCV data for {self.symbol}")
        df = ohlcv_to_dataframe(all_rows)
        df = df[~df.index.duplicated(keep="last")]
        df.sort_index(inplace=True)
        return df

    def load_trend_data(self, days=90, testnet=True):
        tf = self.cfg.get("trend_filter", {}).get("timeframe", "1h")
        return self.load_data(days=days, testnet=testnet, timeframe=tf)

    def load_funding_history(self, days=90, testnet=True):
        """Riwayat funding rate Binance via ccxt.fetch_funding_rate_history.
        Perlu mainnet (funding riil), bukan testnet."""
        events = []
        ex = ExchangeClient(name=self.cfg["exchange"]["name"], testnet=testnet,
                            options=self.cfg["exchange"].get("options", {}))
        since = int((time.time() - days * 86400) * 1000)
        end = int(time.time() * 1000)
        try:
            while since < end:
                rows = ex.client.fetch_funding_rate_history(self.symbol, since=since, limit=1000)
                if not rows:
                    break
                for r in rows:
                    ts = r.get("timestamp")
                    rate = r.get("fundingRate")
                    if ts is None or rate is None:
                        continue
                    events.append((int(ts), float(rate)))
                last = rows[-1].get("timestamp")
                if not last or last <= since:
                    break
                since = last + 1
        except Exception as e:
            logger = logging.getLogger("backtest")
            logger.warning("funding history unavailable for %s: %s", self.symbol, e)
        finally:
            ex.client.close()
        events.sort(key=lambda x: x[0])
        self._funding = events
        return events

    @staticmethod
    def _index_ms(index):
        """DatetimeIndex -> epoch milidetik. Toleran terhadap resolusi ns/ms."""
        vals = index.astype("int64")
        if vals.max() > 1e16:  # nanosecond resolution
            vals = vals // 10**6
        return vals

    def _build_funding_bar(self, df):
        """sum funding rate per bar strategi: bar i diberi rate utk jendela (open[i-1], open[i]]."""
        n = len(df)
        arr = np.zeros(n)
        if not self._funding:
            self._funding_bar = arr
            return arr
        ts_ms = self._index_ms(df.index)
        ev_ts = np.array([e[0] for e in self._funding], dtype="int64")
        ev_r = np.array([e[1] for e in self._funding], dtype="float64")
        for i in range(1, n):
            lo = int(ts_ms[i - 1])
            hi = int(ts_ms[i])
            mask = (ev_ts > lo) & (ev_ts <= hi)
            if mask.any():
                arr[i] = ev_r[mask].sum()
        self._funding_bar = arr
        return arr

    # ------------------------------------------------------------------ trend
    def _align_trend(self, df, trend_df, adx_period, trend_fast, trend_slow):
        """Map 1h ADX & trend EMA ke tiap bar timeframe strategi, tanpa lookahead:
        untuk bar strategi di dalam candle 1h yang masih berjalan, pakai 1h bar
        sebelumnya (yang sudah closed)."""
        adx = compute_adx(trend_df, adx_period)
        tf_fast = compute_ema(trend_df["close"], trend_fast)
        tf_slow = compute_ema(trend_df["close"], trend_slow)
        ts_1h = self._index_ms(trend_df.index)
        ts_15m = self._index_ms(df.index)
        j = np.searchsorted(ts_1h, ts_15m, side="right") - 1
        adx_vals = np.full(len(ts_15m), np.nan)
        dir_vals = [None] * len(ts_15m)
        tf_fast_a = tf_fast.values
        tf_slow_a = tf_slow.values
        for i, t in enumerate(ts_15m):
            k = int(j[i])
            if k < 0:
                continue
            if k >= 1 and t - ts_1h[k] < 3600 * 1000:
                k -= 1
            if k < 0:
                continue
            a = adx.iloc[k]
            if not math.isnan(a):
                adx_vals[i] = a
            f = float(tf_fast_a[k])
            s = float(tf_slow_a[k])
            if not math.isnan(f) and not math.isnan(s):
                dir_vals[i] = "long" if f > s else ("short" if f < s else None)
        return adx_vals, dir_vals

    # ------------------------------------------------------------------ run
    def run(self, df, trend_df=None):
        strat = self.cfg["strategies"].get(self.strategy, {})
        if self.strategy == "momentum":
            return self._run_momentum(df, strat, trend_df)
        raise NotImplementedError(f"strategy {self.strategy} not supported yet")

    def _run_momentum(self, df, strat, trend_df=None):
        ema_fast = strat.get("ema_fast", 9)
        ema_slow = strat.get("ema_slow", 21)
        rsi_period = strat.get("rsi_period", 14)
        rsi_oversold = strat.get("rsi_oversold", 30)
        rsi_overbought = strat.get("rsi_overbought", 70)
        require_rsi = strat.get("require_rsi_filter", True)
        cooldown = strat.get("cooldown_seconds", 900) / (CANDLE_TF[self.timeframe] or 900)
        risk = self.cfg["risk"]
        use_atr = risk.get("use_atr_stops", False)
        atr_period = risk.get("atr_period", 14)
        atr_stop = risk.get("atr_stop_mult", 1.5)
        atr_tp = risk.get("atr_tp_mult", 2.5)
        tp_pct = risk.get("take_profit_pct", 2.0) / 100.0
        sl_pct = risk.get("stop_loss_pct", 1.0) / 100.0
        trail_pct = risk.get("trailing_stop_pct", 0.0) / 100.0
        max_pos_pct = risk.get("max_position_pct", 20) / 100.0
        trend_cfg = self.cfg.get("trend_filter", {})
        trend_enabled = trend_cfg.get("enabled", False)
        trend_fast = trend_cfg.get("ema_fast", 21)
        trend_slow = trend_cfg.get("ema_slow", 50)
        trend_tf = trend_cfg.get("timeframe", "1h")
        adx_cfg = trend_cfg.get("adx_filter", {})
        adx_enabled = trend_enabled and adx_cfg.get("enabled", True)
        adx_period = int(adx_cfg.get("period", 14))
        adx_min = float(adx_cfg.get("min_adx", 20))

        close = df["close"]
        fast = compute_ema(close, ema_fast)
        slow = compute_ema(close, ema_slow)
        rsi = compute_rsi(close, rsi_period)
        atr = compute_atr(df, atr_period)

        if trend_enabled and trend_df is not None and trend_tf != self.timeframe:
            adx_vals, dir_vals = self._align_trend(df, trend_df, adx_period, trend_fast, trend_slow)
        elif trend_enabled:
            adx_vals = compute_adx(df, adx_period).values
            tf_fast = compute_ema(close, trend_fast).values
            tf_slow = compute_ema(close, trend_slow).values
            dir_vals = [
                "long" if f > s else ("short" if f < s else None)
                for f, s in zip(tf_fast, tf_slow)
            ]
        else:
            adx_vals = None
            dir_vals = None

        funding_bar = self._build_funding_bar(df)

        warmup = max(ema_slow, rsi_period, atr_period, adx_period) + 3
        equity = self.initial_equity
        peak_equity = equity
        max_dd = 0.0
        pos = None
        cooldown_until = -1
        trades = []
        self._trace = []
        self._trades = trades
        slip = self.slippage
        funding_total = 0.0

        for i in range(warmup, len(df)):
            price = float(close.iloc[i])
            if pos:
                entry, side, qty, best, stop, take, fund_acc = pos
                realized = False
                if side == "long":
                    if price <= stop or (trail_pct and price <= best * (1 - trail_pct)):
                        realized = True
                    elif price >= take:
                        realized = True
                    best = max(best, price)
                else:
                    if price >= stop or (trail_pct and price >= best * (1 + trail_pct)):
                        realized = True
                    elif price <= take:
                        realized = True
                    best = min(best, price)
                if realized:
                    exit_px = price * (1 - slip) if side == "long" else price * (1 + slip)
                    gross = (exit_px - entry) * qty if side == "long" else (entry - exit_px) * qty
                    pnl = gross - self.fee * abs(qty) * (exit_px + entry) + fund_acc
                    equity += pnl
                    trades.append({
                        "entry_idx": entry_i,
                        "exit_idx": i,
                        "side": side,
                        "pnl": pnl,
                        "pnl_pct": pnl / (entry * qty) * 100,
                        "funding": fund_acc,
                    })
                    funding_total += fund_acc
                    pos = None
                else:
                    r = float(funding_bar[i])
                    fund_acc += qty * price * (r if side == "short" else -r)
                    pos = (entry, side, qty, best, stop, take, fund_acc)
                    continue
            if i < len(df) - 1:
                r = float(rsi.iloc[i])
                cross_up = float(fast.iloc[i - 1]) <= float(slow.iloc[i - 1]) and float(fast.iloc[i]) > float(slow.iloc[i])
                cross_dn = float(fast.iloc[i - 1]) >= float(slow.iloc[i - 1]) and float(fast.iloc[i]) < float(slow.iloc[i])
                sig = None
                if cross_up:
                    if not (require_rsi and r >= rsi_overbought):
                        sig = "long"
                elif cross_dn:
                    if not (require_rsi and r <= rsi_oversold):
                        sig = "short"
                if sig and trend_enabled and dir_vals is not None:
                    trend_dir = dir_vals[i]
                    if trend_dir and trend_dir != sig:
                        sig = None
                if sig and adx_enabled:
                    a = float(adx_vals[i])
                    if not math.isnan(a) and a < adx_min:
                        sig = None
                if sig and i >= cooldown_until:
                    if self.order_type == "limit" and self.fill_rate < 1.0:
                        if self.rng.random() > self.fill_rate:
                            sig = None
                if sig and i >= cooldown_until:
                    qty = (equity * max_pos_pct) / price
                    a = float(atr.iloc[i])
                    if use_atr and a > 0:
                        stop = price - atr_stop * a if sig == "long" else price + atr_stop * a
                        take = price + atr_tp * a if sig == "long" else price - atr_tp * a
                    else:
                        stop = price * (1 - sl_pct) if sig == "long" else price * (1 + sl_pct)
                        take = price * (1 + tp_pct) if sig == "long" else price * (1 - tp_pct)
                    if self.order_type == "market":
                        entry = price * (1 + slip) if sig == "long" else price * (1 - slip)
                    else:
                        entry = price  # limit: entry di level order (best bid/ask), tanpa slippage
                    pos = (entry, sig, qty, entry, stop, take, 0.0)
                    entry_i = i
                    cooldown_until = i + int(cooldown)
            peak_equity = max(peak_equity, equity)
            max_dd = max(max_dd, (peak_equity - equity) / peak_equity)

        return self._stats(df, trades, equity, max_dd, funding_total)

    def _stats(self, df, trades, equity, max_dd, funding_total=0.0):
        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        pct = [t["pnl_pct"] for t in trades]
        win_pct = [p for p in pct if p > 0]
        loss_pct = [p for p in pct if p <= 0]
        win_rate = len(wins) / len(pnls) if pnls else 0.0
        avg_win = sum(win_pct) / len(win_pct) if win_pct else 0.0
        avg_loss = sum(loss_pct) / len(loss_pct) if loss_pct else 0.0
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy,
            "order_type": self.order_type,
            "fill_rate": self.fill_rate,
            "slippage_pct": round(self.slippage * 100, 3),
            "bars": len(df),
            "period_days": round(len(df) * (CANDLE_TF[self.timeframe] or 900) / 86400, 1),
            "trades": len(pnls),
            "win_rate": round(win_rate * 100, 1),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "net_pnl": round(equity - self.initial_equity, 2),
            "total_return_pct": round((equity / self.initial_equity - 1) * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "avg_win_pct": round(avg_win, 3),
            "avg_loss_pct": round(avg_loss, 3),
            "expectancy_pct": round(expectancy, 4),
            "funding_total": round(funding_total, 4),
        }


CANDLE_TF = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def _run_backtest(config, symbol, days, equity, order_type, slippage_pct, fill_rate,
                  adx_state, adx_min, use_funding, mainnet, split_test, timeframe, fee):
    adx_cfg = config.setdefault("trend_filter", {}).setdefault("adx_filter", {})
    if adx_state == "on":
        adx_cfg["enabled"] = True
    elif adx_state == "off":
        adx_cfg["enabled"] = False
    if adx_min is not None:
        adx_cfg["min_adx"] = adx_min

    bt = Backtester(config, symbol, timeframe, "momentum", equity, fee=fee,
                    order_type=order_type, slippage_pct=slippage_pct, fill_rate=fill_rate)
    df = bt.load_data(days=days, testnet=not mainnet)
    trend_df = None
    if config.get("trend_filter", {}).get("enabled", False):
        trend_df = bt.load_trend_data(days=days, testnet=not mainnet)
    if use_funding:
        bt.load_funding_history(days=days, testnet=False)

    results = {}
    if split_test and 0 < split_test < 1:
        cut = int(len(df) * (1 - split_test))
        results["train"] = bt.run(df.iloc[:cut], trend_df)
        results["test"] = bt.run(df.iloc[cut:], trend_df)
        results["full"] = bt.run(df, trend_df)
    else:
        results["full"] = bt.run(df, trend_df)
    return results


def main():
    parser = argparse.ArgumentParser(description="Backtest strategi momentum dengan data historis")
    parser.add_argument("--symbol", default="BTC/USDT:USDT")
    parser.add_argument("--symbols", default=None, help="Daftar simbol pisah koma; lebih dari satu => tabel perbandingan")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--strategy", default="momentum")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--equity", type=float, default=10000.0)
    parser.add_argument("--fee", type=float, default=None)
    parser.add_argument("--mainnet", action="store_true", help="Pakai mainnet (default testnet)")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--adx-on", action="store_true", help="Paksa ADX filter aktif")
    parser.add_argument("--adx-off", action="store_true", help="Paksa ADX filter mati")
    parser.add_argument("--adx-min", type=float, default=None, help="Override min_adx")
    parser.add_argument("--order-type", choices=["market", "limit"], default="market")
    parser.add_argument("--fill-rate", type=float, default=None, help="Override fill rate limit order (0-1)")
    parser.add_argument("--slippage-pct", type=float, default=0.05)
    parser.add_argument("--split-test", type=float, default=0.3, help="Fraksi data terakhir sebagai test set (0=full only)")
    parser.add_argument("--no-funding", action="store_true", help="Nonaktifkan simulasi biaya funding")
    parser.add_argument("--compare", action="store_true", help="Bandingkan market vs limit utk semua simbol")
    parser.add_argument("--json", default=None, help="Simpan hasil ke file json")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = [args.symbol]
    adx_state = "on" if args.adx_on else ("off" if args.adx_off else "as-is")
    adx_min = args.adx_min
    use_funding = not args.no_funding

    if args.compare:
        import collections
        out = collections.defaultdict(dict)
        for sym in symbols:
            for ot in ("market", "limit"):
                for adx in ("on", "off"):
                    res = _run_backtest(config, sym, args.days, args.equity, ot, args.slippage_pct,
                                        args.fill_rate, adx, adx_min, use_funding, args.mainnet,
                                        args.split_test, args.timeframe, args.fee)
                    out[(sym, adx)][ot] = res
        _print_compare(out, symbols)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({f"{s}|{a}": v for (s, a), v in out.items()}, f, indent=2)
        return

    adx_cfg = config.setdefault("trend_filter", {}).setdefault("adx_filter", {})
    if args.adx_on:
        adx_cfg["enabled"] = True
    elif args.adx_off:
        adx_cfg["enabled"] = False
    if adx_min is not None:
        adx_cfg["min_adx"] = adx_min

    for sym in symbols:
        bt = Backtester(config, sym, args.timeframe, args.strategy, args.equity, fee=args.fee,
                        order_type=args.order_type, slippage_pct=args.slippage_pct, fill_rate=args.fill_rate)
        df = bt.load_data(days=args.days, testnet=not args.mainnet)
        trend_df = None
        if config.get("trend_filter", {}).get("enabled", False):
            trend_df = bt.load_trend_data(days=args.days, testnet=not args.mainnet)
        if use_funding:
            bt.load_funding_history(days=args.days, testnet=False)
        if args.split_test and 0 < args.split_test < 1:
            cut = int(len(df) * (1 - args.split_test))
            for label, seg in (("train", df.iloc[:cut]), ("test", df.iloc[cut:]), ("full", df)):
                print(f"=== {sym} [{label}] ===")
                for k, v in bt.run(seg, trend_df).items():
                    print(f"  {k}: {v}")
        else:
            print(f"=== {sym} ===")
            for k, v in bt.run(df, trend_df).items():
                print(f"  {k}: {v}")


def _print_compare(out, symbols):
    def row(r):
        if not r:
            return None
        return r

    print("\n==================== PERBANDINGAN MARKET vs LIMIT ====================")
    for sym in symbols:
        for adx in ("on", "off"):
            d = out.get((sym, adx), {})
            print(f"\n### {sym}  |  ADX={'ON' if adx=='on' else 'OFF'}")
            header = f"{'seg':<6} {'ord':<7} {'tr':>4} {'win%':>6} {'avgW%':>7} {'avgL%':>7} {'exp%':>8} {'fund':>8} {'net$':>9} {'ret%':>7} {'mdd%':>6}"
            print(header)
            for seg in ("train", "test", "full"):
                for ot in ("market", "limit"):
                    r = d.get(ot, {}).get(seg)
                    if not r:
                        continue
                    print(f"{seg:<6} {ot:<7} {r['trades']:>4} {r['win_rate']:>6} {r['avg_win_pct']:>7} {r['avg_loss_pct']:>7} "
                          f"{r['expectancy_pct']:>8} {r['funding_total']:>8} {r['net_pnl']:>9} {r['total_return_pct']:>7} {r['max_drawdown_pct']:>6}")
    # ringkasan expectancy rata-rata
    print("\n--- RINGKASAN expectancy (%/trade, rata-rata antar simbol, segmen FULL) ---")
    for adx in ("on", "off"):
        ex_m = []
        ex_l = []
        tr_m = 0
        tr_l = 0
        for sym in symbols:
            d = out.get((sym, adx), {})
            rm = d.get("market", {}).get("full")
            rl = d.get("limit", {}).get("full")
            if rm:
                ex_m.append(rm["expectancy_pct"])
                tr_m += rm["trades"]
            if rl:
                ex_l.append(rl["expectancy_pct"])
                tr_l += rl["trades"]
        print(f"ADX={adx.upper()}: market mean exp = {sum(ex_m)/len(ex_m) if ex_m else 0:.4f}% (trades {tr_m}) | "
              f"limit mean exp = {sum(ex_l)/len(ex_l) if ex_l else 0:.4f}% (trades {tr_l})")


if __name__ == "__main__":
    main()
