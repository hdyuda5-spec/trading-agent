import re, datetime, json

LOG = "/home/ubuntu/trading-agent/logs/agent.log"

sig_re = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| \w+ \| trading-agent \| "
    r".*SINYAL (BUY|SELL) — (?P<sym>[A-Z0-9]+/USDT).*Entry: (?P<entry>[\d.,]+)"
)

signals = []
with open(LOG) as f:
    for line in f:
        m = sig_re.search(line)
        if not m:
            continue
        ts = datetime.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        if ts < datetime.datetime(2026, 8, 4, 16, 5):
            continue
        signals.append({
            "ts": ts,
            "ts_unix": int(ts.timestamp()),
            "side": m.group(2),
            "sym": m.group(3),
            "entry": float(m.group(4).replace(",", "")),
        })

from collections import Counter
c = Counter(s["sym"] for s in signals)
print("total signals:", len(signals))
for sym, n in c.most_common():
    print(f"  {sym}: {n}")

with open("/tmp/opencode/signals.json", "w") as f:
    json.dump(signals, f, indent=1, default=str)
