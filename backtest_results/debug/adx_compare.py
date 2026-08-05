import copy
import json
import os
import sys

sys.path.insert(0, "/home/ubuntu/trading-agent")

from backtest import Backtester

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "SNDK/USDT:USDT",
    "KORU/USDT:USDT",
    "XAU/USDT:USDT",
    "CL/USDT:USDT",
    "MU/USDT:USDT",
    "ZEC/USDT:USDT",
    "SKHY/USDT:USDT",
    "HYPE/USDT:USDT",
]

with open("/home/ubuntu/trading-agent/config.json") as f:
    base_cfg = json.load(f)

print(f"{'symbol':<18} {'mode':<4} {'bars':>6} {'days':>6} {'trd':>4} {'win%':>6} {'PF':>5} {'netPnL':>9} {'maxDD%':>7}")
for sym in SYMBOLS:
    bt = Backtester(base_cfg, sym, "15m", "momentum", 10000.0, 0.0002)
    try:
        df = bt.load_data(days=60, testnet=False)
    except Exception as e:
        print(f"{sym:<18} DATA ERROR: {e}")
        continue
    for mode in ("off", "on"):
        cfg = copy.deepcopy(base_cfg)
        cfg["trend_filter"]["adx_filter"]["enabled"] = mode == "on"
        r = Backtester(cfg, sym, "15m", "momentum", 10000.0, 0.0002).run(df)
        print(
            f"{sym.replace('/USDT:USDT',''):<18} {mode:<4} {r['bars']:>6} {r['period_days']:>6.1f} "
            f"{r['trades']:>4} {r['win_rate']:>6.1f} {str(r['profit_factor']):>5} {r['net_pnl']:>9.2f} {r['max_drawdown_pct']:>7.2f}"
        )
