import json, sys
sys.path.insert(0, "/tmp/opencode")
sys.path.insert(0, "/home/ubuntu/trading-agent")
import backtest_trace as btm
from backtest_trace import Backtester

config = json.load(open("/home/ubuntu/trading-agent/config.json"))
adx_cfg = config["trend_filter"]["adx_filter"]; adx_cfg["enabled"] = True; adx_cfg["min_adx"] = 20
sym = "BTC/USDT:USDT"

bt = Backtester(config, sym, "15m", "momentum", 10000.0, fee=None, order_type="limit", slippage_pct=0.05, fill_rate=0.476)
df = bt.load_data(days=90, testnet=False)
trend_df = bt.load_trend_data(days=90, testnet=False)
bt.load_funding_history(days=90, testnet=False)
cut = int(len(df) * 0.7)
print(f"bars={len(df)} cut={cut}")

def grab(label, seg, offset=0):
    r = bt.run(seg, trend_df)
    trs = [(t["entry_idx"] + offset, t["exit_idx"] + offset) for t in bt._trades]
    print(f"{label}: trades={r['trades']} exp={r['expectancy_pct']:.4f}% net={r['net_pnl']}")
    return trs

t_train = grab("train", df.iloc[:cut])
t_test = grab("test", df.iloc[cut:], offset=cut)
t_full = grab("full", df)

print(f"\ncounts: full={len(t_full)} train={len(t_train)} test={len(t_test)} | train+test={len(t_train)+len(t_test)}")
print(f"=> selisih full - (train+test) = {len(t_full) - len(t_train) - len(t_test)}")

cross = [e for e in t_full if e[0] < cut and e[1] >= cut]
print(f"\n[A] full trades OPEN di train, CLOSE di test (posisi nyambung melewati cut): {len(cross)}")
for e in cross:
    print(f"    entry={e[0]} (bar {cut-e[0]} sblm akhir train) -> exit={e[1]} (bar {e[1]-cut} di test)")

full_train = [e for e in t_full if e[1] < cut]
full_test = [e for e in t_full if e[0] >= cut]
only_full_train = [e for e in full_train if e not in t_train]
only_full_test = [e for e in full_test if e not in t_test]
print(f"[B] full trades seluruhnya di train range: {len(full_train)}; TIDAK muncul di train-seg run: {len(only_full_train)}")
print(f"[C] full trades seluruhnya di test range: {len(full_test)}; TIDAK muncul di test-seg run: {len(only_full_test)}")
extra = [e for e in t_train + t_test if e not in t_full]
print(f"[D] trade dari train/test-seg yang TIDAK ada di full run: {len(extra)} e.g. {extra[:10]}")

# Cooldown carry: signal tepat sebelum cut menekan signal di awal test pada full run.
print(f"\n[E] full: entry di 25 bar pertama test window = {len([e for e in t_full if cut <= e[0] < cut+25])}")
print(f"    test-seg: entry di 25 bar pertama = {len([e for e in t_test if cut <= e[0] < cut+25])}")
# di mana perbedaan muncul di test window?
print("\nper bandingan entry-ts di window test (full vs isolated):")
full_ts = sorted(e[0] for e in full_test)
seg_ts = sorted(e[0] for e in t_test)
print(f"    full:    {full_ts}")
print(f"    isolated:{seg_ts}")
