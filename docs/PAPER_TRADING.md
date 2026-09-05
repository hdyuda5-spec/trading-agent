# Paper Trading — zero-risk execution mode

Paper mode is the **default** (`config.json → trading.mode = "paper"`). The
whole stack runs unchanged: real market data, simulated money.

## How it works

`TradingBot` wraps the real exchange (`ExchangeClient`) in `PaperExchange`
(`agent/execution/paper.py`):

| Concern | Paper behavior |
|---|---|
| Prices / books / OHLCV / tickers | delegated live to the real exchange |
| Balances | simulated `initial_balance_usdt` (default 1000) |
| Positions | simulated: long/short, average entry, unrealized mark |
| Orders | simulated fills at live/limit price, fee `fee_pct` |
| Stops (algo) | simulated reduce-only stop orders, cancellable |
| Leverage / margin mode | stubbed no-ops (doesn't touch the account) |

Accounting is **equity-based** (like a real futures account): opening a
position doesn't change the balance; closing realizes PnL:
`balance += (exit−entry) × contracts − fees`.

## Switching to live

Live is a deliberate, two-step change — and even then nothing trades unless
`auto_trade` is also enabled:

1. `config.json`: `trading.mode = "live"`
2. `config.json`: `screener.auto_trade = true`  ← **requires explicit sign-off**
3. Fill `.env` with real exchange keys
4. Confirm `python main.py --check` shows the expected balance

`agent/core/config_validate.py` *warns* when live + auto_trade are both on.

## Monitoring

* `/paper` in Telegram → balance, positions, open orders, current mode
* `/funnel`, `/decisions`, `/missed` → the decision trail behind every ticket

## Safety notes

* Paper mode never touches real funds even with real API keys in `.env`.
* `auto_trade` defaults to `false` and is currently `false` in `config.json`.
* The TradeStore v2 tables (`tickets`, `decisions`, `missed_trades`,
  `telemetry`) write in both modes, so paper journals become the training data
  for tuning thresholds.