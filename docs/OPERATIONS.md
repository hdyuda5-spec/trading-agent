# Operations — run, monitor, tune

## Run (pm2 uses `ecosystem.config.js`)

```bash
pm2 start ecosystem.config.js --only trading-agent   # paper mode default
pm2 logs trading-agent                               # logs/ via ecosystem.config.js
pm2 restart trading-agent && pm2 save
```

Logs go to `logs/pm2_out.log` / `logs/pm2_error.log` (repo-local; never
`/var/log`). `TradingBot` runs an event-driven loop: WebSocket feed →
CandleStore (max 800 bars) → features → Decision Engine → Risk → Execution;
the management loop only refreshes equity/positions, SL/TP guards, stale-order
sweeps and scheduled jobs (screen / whale / daily report).

## Health gates before anything trades

1. `main.py` runs `validate_or_raise(config)` — fail fast, Indonesian messages.
2. Exchange health check → abort if unreachable.
3. `min_equity_usdt` → trading paused until balance is topped up.
4. `decision_engine` hard vetoes always win (invalid price/SL-TP, daily loss,
   halt, exchange down).
5. `screener.auto_trade = false` — no auto-entries regardless of signals.

## Decision tuning (paper-first workflow)

1. Let paper mode run; watch `/funnel` (signal funnel) and `/reject`.
2. Raise `decision.min_margin` (currently 0.15) to make WAIT more aggressive.
3. Raise `thresholds.long/short` to require stronger evidence for PASS.
4. Steepen `RiskEngine` with `risk_per_trade_pct` / `min_rr`.
5. Only after sustained paper results → revisit live mode.

## Recurring jobs

| Job | Cadence | Notes |
|---|---|---|
| Auto-screen | `screener.auto_interval_minutes` (30) | signal cards + (off) auto-trade |
| Whale scan | whale service | detected once per interval |
| Daily report | `reporting.daily_hour` (08:00 WIB) | includes funnel + decision stats |

## Telegram commands (new)

`/funnel` funnel counts · `/reject` top rejection reasons · `/decisions`
decision trail · `/missed` missed-trade journal · `/paper` mode + paper balance

## Fail-fast invariants

* Invalid config → `ConfigError` before start, no partial boot.
* Secrets are never logged; `agent.core.secrets.redact_*` masks long token
  shapes and known env values (`BINANCE_*`, `TELEGRAM_*`, `*_API_KEY`), paired
  with `tests/test_hardening.py`.
* If you see `screener.auto_trade=true` in any diff or log line, stop and
  review — the safest value is `false`.