import json, sys, time
sys.path.insert(0, "/home/ubuntu/trading-agent")
from agent.core.exchange import ExchangeClient

config = json.load(open("/home/ubuntu/trading-agent/config.json"))
ex = ExchangeClient(name=config["exchange"]["name"], testnet=False,
                    options=config["exchange"].get("options", {}))
symbols = ["BTC/USDT:USDT","ETH/USDT:USDT","SOL/USDT:USDT","SNDK/USDT:USDT","KORU/USDT:USDT",
           "XAU/USDT:USDT","CL/USDT:USDT","MU/USDT:USDT","ZEC/USDT:USDT","SKHY/USDT:USDT","HYPE/USDT:USDT"]
print(f"{'symbol':<18}{'earliest_utc':<22}{'days_cov':<9}{'bars15m'}")
for sym in symbols:
    now = int(time.time()*1000)
    since = now - 400*86400*1000
    rows = ex.fetch_ohlcv(sym, "15m", since=since, limit=1000)
    if not rows:
        print(f"{sym:<18} NO DATA")
        continue
    first = rows[0][0]
    days_cov = (now - first)/86400000
    print(f"{sym:<18}{time.strftime('%Y-%m-%d %H:%M', time.gmtime(first/1000)):<22}{days_cov:<9.1f}{len(rows)}")
ex.client.close()
