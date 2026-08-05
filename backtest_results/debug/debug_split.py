import json, sys, math
import numpy as np
sys.path.insert(0, "/home/ubuntu/trading-agent")
import backtest as bt

config = json.load(open("/home/ubuntu/trading-agent/config.json"))
adx_cfg = config["trend_filter"]["adx_filter"]
adx_cfg["enabled"] = True
adx_cfg["min_adx"] = 20

sym = "BTC/USDT:USDT"
b = bt.Backtester(config, sym, "15m", "momentum", 10000.0, fee=None, order_type="limit", slippage_pct=0.05, fill_rate=0.476)
df = b.load_data(days=90, testnet=False)
trend_df = b.load_trend_data(days=90, testnet=False)

cut = int(len(df) * 0.7)
print(f"total bars={len(df)} cut(train end)={cut} test bars={len(df)-cut}")


def run_traced(df_seg, base_idx):
    strat = config["strategies"]["momentum"]
    risk = config["risk"]
    trend_cfg = config["trend_filter"]
    trend_enabled = trend_cfg.get("enabled", False)
    trend_fast = trend_cfg.get("ema_fast", 21)
    trend_slow = trend_cfg.get("ema_slow", 50)
    trend_tf = trend_cfg.get("timeframe", "1h")
    ema_fast = strat.get("ema_fast", 9); ema_slow = strat.get("ema_slow", 21)
    rsi_period = strat.get("rsi_period", 14)
    rsi_oversold = strat.get("rsi_oversold", 30); rsi_overbought = strat.get("rsi_overbought", 70)
    require_rsi = strat.get("require_rsi_filter", True)
    cooldown = strat.get("cooldown_seconds", 900) / 900
    use_atr = risk.get("use_atr_stops", False)
    atr_period = risk.get("atr_period", 14)
    atr_stop = risk.get("atr_stop_mult", 1.5); atr_tp = risk.get("atr_tp_mult", 2.5)
    tp_pct = risk.get("take_profit_pct", 2.0)/100; sl_pct = risk.get("stop_loss_pct", 1.0)/100
    trail_pct = risk.get("trailing_stop_pct", 0.0)/100
    max_pos_pct = risk.get("max_position_pct", 20)/100
    adx_period = int(adx_cfg.get("period", 14)); adx_min = float(adx_cfg.get("min_adx", 20))
    adx_enabled = True

    close = df_seg["close"]
    fast = compute_ema(close, ema_fast); slow = compute_ema(close, ema_slow)
    rsi = compute_rsi(close, rsi_period); atr = compute_atr(df_seg, atr_period)
    adx_vals, dir_vals = b._align_trend(df_seg, trend_df, adx_period, trend_fast, trend_slow)
    warmup = max(ema_slow, rsi_period, atr_period, adx_period) + 3
    pos = None; cooldown_until = -1
    trades = []
    for i in range(warmup, len(df_seg)):
        price = float(close.iloc[i])
        if pos:
            entry, side, qty, best, stop, take, fund_acc, entry_idx = pos
            realized = False
            if side == "long":
                if price <= stop or (trail_pct and price <= best*(1-trail_pct)): realized=True
                elif price >= take: realized=True
                best = max(best, price)
            else:
                if price >= stop or (trail_pct and price >= best*(1+trail_pct)): realized=True
                elif price <= take: realized=True
                best = min(best, price)
            if realized:
                exit_px = price*(1-b.slippage) if side=="long" else price*(1+b.slippage)
                gross = (exit_px-entry)*qty if side=="long" else (entry-exit_px)*qty
                pnl = gross - b.fee*abs(qty)*(exit_px+entry) + fund_acc
                trades.append({"entry_idx": base_idx+entry_idx, "exit_idx": base_idx+i, "side": side,
                               "pnl_pct": pnl/(entry*qty)*100})
                pos = None
            else:
                pos = (entry, side, qty, best, stop, take, fund_acc, entry_idx)
                continue
        if i < len(df_seg)-1:
            r = float(rsi.iloc[i])
            cross_up = float(fast.iloc[i-1]) <= float(slow.iloc[i-1]) and float(fast.iloc[i]) > float(slow.iloc[i])
            cross_dn = float(fast.iloc[i-1]) >= float(slow.iloc[i-1]) and float(fast.iloc[i]) < float(slow.iloc[i])
            sig = None
            if cross_up:
                if not (require_rsi and r >= rsi_overbought): sig="long"
            elif cross_dn:
                if not (require_rsi and r <= rsi_oversold): sig="short"
            if sig and trend_enabled and dir_vals is not None:
                td = dir_vals[i]
                if td and td != sig: sig=None
            if sig and adx_enabled and not math.isnan(float(adx_vals[i])):
                if float(adx_vals[i]) < adx_min: sig=None
            if sig and i >= cooldown_until:
                if b.order_type=="limit" and b.fill_rate < 1.0 and b.rng.random() > b.fill_rate:
                    sig = None
            if sig and i >= cooldown_until:
                a = float(atr.iloc[i])
                if use_atr and a > 0:
                    stop = price - atr_stop*a if sig=="long" else price + atr_stop*a
                    take = price + atr_tp*a if sig=="long" else price - atr_tp*a
                else:
                    stop = price*(1-sl_pct) if sig=="long" else price*(1+sl_pct)
                    take = price*(1+tp_pct) if sig=="long" else price*(1-tp_pct)
                entry = price if b.order_type=="limit" else (price*(1+b.slippage) if sig=="long" else price*(1-b.slippage))
                pos = (entry, sig, 1.0, entry, stop, take, 0.0, i)
                cooldown_until = i + int(cooldown)
    return trades


def compute_ema(s, p): return s.ewm(span=p, adjust=False).mean()
def compute_rsi(s, p):
    d = s.diff(); g = d.clip(lower=0).rolling(p).mean(); l = -d.clip(upper=0).rolling(p).mean()
    rs = g/l; return 100 - 100/(1+rs)
def compute_atr(df_, p):
    h=df_["high"]; l=df_["low"]; c=df_["close"]
    tr = (h-l).combine((h-c.shift()).abs(), max).combine((l-c.shift()).abs(), max)
    return tr.ewm(alpha=1/p, adjust=False).mean()

full = run_traced(df, 0)
train = run_traced(df.iloc[:cut], 0)
test = run_traced(df.iloc[cut:], cut)

print(f"\nfull trades={len(full)}, train-seg={len(train)}, test-seg={len(test)}")
cross = [t for t in full if t["entry_idx"] < cut and t["exit_idx"] >= cut]
print(f"full trades CROSSING boundary (opened in train, closed in test): {len(cross)}")
for t in cross:
    print(f"  entry_idx={t['entry_idx']} (bar {t['entry_idx']-cut} sebelum cut end) exit_idx={t['exit_idx']} pnl_pct={t['pnl_pct']:+.3f}%")

early = [t for t in full if cut <= t["entry_idx"] < cut+20]
print(f"\nfull trades opened in first 20 bars of test window: {len(early)}")
for t in early[:8]:
    print(f"  entry_idx={t['entry_idx']} (bar {t['entry_idx']-cut} of test) pnl_pct={t['pnl_pct']:+.3f}%")

seg_test = [t for t in test]
print(f"\ntest-segment entries={len(seg_test)}")
print("difference (full - train - test) =", len(full)-len(train)-len(test))
