import json, sys, datetime, time
from datetime import timezone, timedelta
import pandas as pd
sys.path.insert(0, "/home/ubuntu/trading-agent")
from agent.core.exchange import ExchangeClient
from agent.core.utils import ohlcv_to_dataframe

WIB = timezone(timedelta(hours=7))

symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SNDK/USDT:USDT", "SOXL/USDT:USDT", "BNB/USDT:USDT"]
spreads = {"BTC/USDT:USDT": 0.000002, "ETH/USDT:USDT": 0.000005, "SNDK/USDT:USDT": 0.000007,
           "SOXL/USDT:USDT": 0.000071, "BNB/USDT:USDT": 0.000017}
default_half = 0.00005

with open("/tmp/opencode/signals.json") as f:
    signals = json.load(f)
for s in signals:
    s["ts"] = datetime.datetime.strptime(s["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=WIB)
    s["ts_unix"] = int(s["ts"].timestamp())

ex = ExchangeClient(name="binance", testnet=False, options={'defaultType':'swap','defaultSubType':'linear','adjustForTimeDifference':True})
data = {}
for sym in symbols:
    rows = []
    start = int((datetime.datetime(2026, 8, 4, 16, 5).timestamp()) * 1000)
    end = int(time.time() * 1000)
    while start < end:
        try:
            batch = ex.fetch_ohlcv(sym, "1m", since=start, limit=1000)
        except Exception as e:
            print(sym, "fetch err", str(e)[:60]); break
        if not batch:
            break
        rows.extend(batch)
        last = batch[-1][0]
        if last <= start:
            break
        start = last + 1
    if rows:
        df = ohlcv_to_dataframe(rows)
        df = df[~df.index.duplicated(keep="last")]
        df.sort_index(inplace=True)
        data[sym] = df
        print(sym, "candles:", len(df), df.index.min(), "->", df.index.max())
    else:
        print(sym, "NO DATA")
ex.client.close()

res = []
for s in signals:
    sym = s["sym"] + ":USDT"
    df = data.get(sym)
    if df is None or df.empty:
        continue
    idx_ms = df.index.astype("int64")
    if idx_ms.max() > 1e15:
        idx_ms = idx_ms // 10**6
    ts = s["ts_unix"] * 1000
    half = default_half if sym not in spreads else spreads[sym] / 2
    if s["side"] == "BUY":
        limit = s["entry"] * (1 - half)
        fills_at = df[(idx_ms >= ts) & (idx_ms < ts + 180000) & (df["low"] <= limit)]
    else:
        limit = s["entry"] * (1 + half)
        fills_at = df[(idx_ms >= ts) & (idx_ms < ts + 180000) & (df["high"] >= limit)]
    filled = len(fills_at) > 0
    win = df[(idx_ms >= ts) & (idx_ms < ts + 180000)]
    # forward return 15m after signal (mengukur missed momentum / adverse selection)
    fwd = df[(idx_ms >= ts + 900000) & (idx_ms < ts + 910000)]
    fwd_pct = None
    if len(fwd):
        fclose = float(fwd["close"].iloc[0])
        if s["side"] == "BUY":
            fwd_pct = (fclose - s["entry"]) / s["entry"] * 100
        else:
            fwd_pct = (s["entry"] - fclose) / s["entry"] * 100
    min_dist = None; max_retrace = None
    if len(win):
        if s["side"] == "BUY":
            min_dist = ((win["low"].min() - limit) / limit) * 100
            max_retrace = ((win["close"].max() - s["entry"]) / s["entry"]) * 100
        else:
            min_dist = ((limit - win["high"].max()) / limit) * 100
            max_retrace = ((s["entry"] - win["close"].min()) / s["entry"]) * 100
    res.append({"ts": s["ts"].isoformat(), "sym": sym, "side": s["side"], "entry": s["entry"],
                "limit": limit, "filled": filled, "min_dist_pct": round(min_dist, 4) if min_dist is not None else None,
                "max_move_pct": round(max_retrace, 4) if max_retrace is not None else None,
                "fwd_15m_pct": round(fwd_pct, 4) if fwd_pct is not None else None})

filled = sum(1 for r in res if r["filled"])
print(f"\n=== FILL RATE: {filled}/{len(res)} = {100*filled/len(res):.1f}% ===")
from collections import defaultdict
by = defaultdict(lambda: [0, 0])
for r in res:
    by[r["sym"]][1] += 1
    if r["filled"]:
        by[r["sym"]][0] += 1
for sym in sorted(by):
    f, t = by[sym]
    print(f"  {sym}: {f}/{t} = {100*f/t:.1f}%")
print("\nmin_dist (untuk yang gagal, seberapa jauh harga dari limit):")
fails = [r for r in res if not r["filled"] and r["min_dist_pct"] is not None]
if fails:
    import statistics as st
    vals = [r["min_dist_pct"] for r in fails]
    print(f"  n={len(fails)} mean={st.mean(vals):.3f}% median={st.median(vals):.3f}% max={max(vals):.3f}%")
    far = sum(1 for r in fails if r["min_dist_pct"] > 0.1)
    print(f"  bergerak MENJAUH >0.1%: {far}/{len(fails)} = {100*far/len(fails):.1f}%")
    approached = sum(1 for r in fails if r["min_dist_pct"] <= 0.01)
    print(f"  sempat mendekat <=0.01% tapi tak cukup: {approached}/{len(fails)}")
    missed = [r for r in fails if r["max_move_pct"] is not None]
    if missed:
        mv = [r["max_move_pct"] for r in missed]
        print(f"  max pergerakan searah signal (fails): mean={st.mean(mv):.3f}% median={st.median(mv):.3f}%")
fwd_f = [r["fwd_15m_pct"] for r in res if r["filled"] and r["fwd_15m_pct"] is not None]
fwd_n = [r["fwd_15m_pct"] for r in res if not r["filled"] and r["fwd_15m_pct"] is not None]
if fwd_f:
    print(f"  fwd 15m FILLED  (n={len(fwd_f)}): mean={st.mean(fwd_f):+.3f}%")
if fwd_n:
    print(f"  fwd 15m NO-FILL (n={len(fwd_n)}): mean={st.mean(fwd_n):+.3f}% (peluang terlewat)")
json.dump(res, open("/tmp/opencode/fills.json", "w"), indent=1, default=str)
