import json, sys
sys.path.insert(0, "/tmp/opencode")
sys.path.insert(0, "/home/ubuntu/trading-agent")
import backtest_trace as btm
from backtest_trace import Backtester

config = json.load(open("/home/ubuntu/trading-agent/config.json"))
adx_cfg = config["trend_filter"]["adx_filter"]; adx_cfg["enabled"] = True; adx_cfg["min_adx"] = 20

sym = "BTC/USDT:USDT"

def run(seg_df, label):
    b = Backtester(config, sym, "15m", "momentum", 10000.0, fee=None, order_type="limit", slippage_pct=0.05, fill_rate=0.476)
    b.load_funding_history(days=90, testnet=False)
    r = b.run(seg_df, trend_df)
    print(f"{label}: trades={r['trades']} exp={r['expectancy_pct']:.4f}% net={r['net_pnl']}")
    return [(t["entry_idx"], t["exit_idx"]) for t in b._trace]

bt0 = Backtester(config, sym)
df = bt0.load_data(days=90, testnet=False)
trend_df = bt0.load_trend_data(days=90, testnet=False)
cut = int(len(df) * 0.7)
print(f"bars={len(df)} cut={cut}")

t_full = run(df, "full")
t_train = run(df.iloc[:cut], "train")
t_test = run(df.iloc[cut:], "test")

print(f"\ncounts: full={len(t_full)} train={len(t_train)} test={len(t_test)} | train+test={len(t_train)+len(t_test)}")
cross = [e for e in t_full if e[0] < cut and e[1] >= cut]
print(f"full trades OPEN in train, CLOSE in test (crossing cut): {len(cross)}")
for e in cross:
    print(f"   entry_idx={e[0]} (bar {cut-e[0]} before end of train) -> exit_idx={e[1]} (bar {e[1]-cut} into test)")

full_train_range = [e for e in t_full if e[1] < cut]
full_test_range = [e for e in t_full if e[0] >= cut]
only_full_train = [e for e in full_train_range if e not in t_train]
only_full_test = [e for e in full_test_range if e not in t_test]
print(f"\nfull trades fully within train range: {len(full_train_range)}, not in train-seg run: {len(only_full_train)}")
print(f"full trades fully within test range: {len(full_test_range)}, not in test-seg run: {len(only_full_test)}")
extra = [e for e in t_train + t_test if e not in t_full]
print(f"seg-run trades NOT in full run: {len(extra)} e.g. {extra[:8]}")
