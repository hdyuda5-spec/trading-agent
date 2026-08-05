import json, sys, math
import numpy as np
sys.path.insert(0, "/home/ubuntu/trading-agent")
import backtest as bt
from backtest import Backtester
from agent.core.utils import compute_adx, compute_atr, compute_ema, compute_rsi

# Reuse the real _run_momentum but add per-trade entry/exit bar index tracing.
_orig = Backtester._run_momentum.__code__
src = Backtester._run_momentum.__code__.co_filename
import inspect
real_src = inspect.getsource(Backtester._run_momentum)

# Build a traced copy by string surgery: append index to trades + return trace.
traced = real_src.replace(
    'trades.append({',
    'self._trace.append((entry_i, i)); trades.append({'
).replace(
    '    "pnl": pnl,',
    '    "pnl": pnl,'
)
# entry_i must be captured: patch the pos-tuple creation
traced = traced.replace(
    'pos = (entry, sig, qty, entry, stop, take, 0.0)',
    'pos = (entry, sig, qty, entry, stop, take, 0.0); entry_i = i'
)

ns = {}
exec(traced, globals(), ns)
Backtester._run_momentum = ns['_run_momentum']

def new_init(self, config, symbol, timeframe="15m", strategy="momentum", initial_equity=10000.0,
             fee=None, order_type="market", slippage_pct=0.05, fill_rate=None, rng_seed=20260805):
    bt.Backtester.__init__(self, config, symbol, timeframe, strategy, initial_equity,
                           fee, order_type, slippage_pct, fill_rate, rng_seed)
    self._trace = []
Backtester.__init__ = new_init

config = json.load(open("/home/ubuntu/trading-agent/config.json"))
adx_cfg = config["trend_filter"]["adx_filter"]; adx_cfg["enabled"] = True; adx_cfg["min_adx"] = 20

sym = "BTC/USDT:USDT"
def run(seg_df, label):
    b = Backtester(config, sym, "15m", "momentum", 10000.0, fee=None, order_type="limit", slippage_pct=0.05, fill_rate=0.476)
    b.load_funding_history(days=90, testnet=False)
    r = b.run(seg_df, trend_df)
    print(f"{label}: trades={r['trades']} exp={r['expectancy_pct']:.4f}% net={r['net_pnl']}")
    return b._trace

df = bt.Backtester.load_data(Backtester(config, sym), days=90, testnet=False)
trend_df = bt.Backtester.load_trend_data(Backtester(config, sym), days=90, testnet=False)
cut = int(len(df) * 0.7)
print(f"bars={len(df)} cut={cut}")

t_full = run(df, "full")
t_train = run(df.iloc[:cut], "train")
t_test = run(df.iloc[cut:], "test")

print(f"\ncounts: full={len(t_full)} train={len(t_train)} test={len(t_test)} | train+test={len(t_train)+len(t_test)}")
cross = [e for e in t_full if e[0] < cut and e[1] >= cut]
print(f"full trades OPEN in train, CLOSE in test (crossing boundary): {len(cross)}")
for e in cross:
    print(f"   entry_idx={e[0]} exit_idx={e[1]}")
open_at_cut = [e for e in t_full if e[0] < cut and e[1] >= cut]
only_full_in_train = [e for e in t_full if e[1] < cut and e not in t_train]
only_full_in_test = [e for e in t_full if e[0] >= cut and e not in t_test]
print(f"full trades in train range NOT in train-seg run: {len(only_full_in_train)}")
print(f"full trades in test range NOT in test-seg run: {len(only_full_in_test)}")
extra_in_segs = [e for e in (t_train + t_test) if e not in t_full]
print(f"trades in seg-runs NOT in full run: {len(extra_in_segs)} (contoh: {extra_in_segs[:6]})")
