# Execution — order lifecycle, stale orders, idempotency, paper mode

`agent/execution/` is the **only** place allowed to place orders. Strategies,
the screener and the decision layer emit `TradeTicket`s; the execution layer
acts on them.

## Order lifecycle (`OrderManager`)

```
assess → ticket → open_position(symbol, signal, equity, atr, ticket=ticket)
   ├─ live price guard (invalid → alert, abort)
   ├─ size via RiskEngine (0 → abort)
   ├─ leverage enforce → market or postOnly limit order
   │    └─ idempotency: clientOrderId = "<prefix>-<ticket_id>" (dupe-safe retries)
   ├─ _wait_fill with entry TTL (timeout → cancel pending, ticket → EXPIRED)
   └─ fill → [place SL/TP reduce-only] → ticket.mark_filled → funnel.fills
```

Fill info (order id + exchange order id) is written back into the ticket and
persisted, so every fill is traceable to a decision.

## Stale orders (fixed in this upgrade)

* `cancel_stale_orders(ttl)` now uses `agent.core.timestamps.order_age_seconds`
  — unit-agnostic (ms / s / µs detected from the value itself, no `1e12`
  magic), skips orders without id/symbol, and never cancels reduce-only
  guards.
* **New** `_cancel_stale_stops(ttl)` sweeps orphaned Binance *algo* stops
  (`fetch_open_stop_orders` / `cancel_stop_order`) which live outside the
  regular open-orders feed and could otherwise fire against a reversed
  position.

## SL/TP safety

`ensure_sl_tp` re-arms missing reduce-only take-profits and re-places stop
guards on every management pass; it deliberately refuses when the position is
gone (`ReduceOnly` error paths are swallowed, not alerted).

## Paper trading

With `trading.mode = "paper"` (the **default**), `TradingBot` wraps the real
`ExchangeClient` in `PaperExchange` (`agent/execution/paper.py`): all market
data (prices, books, OHLCV) is live/real; the account (balance, positions,
orders) is simulated and starts at `initial_balance_usdt`. Set
`trading.mode = "live"` **only** deliberately — live still requires
`screener.auto_trade = true` (which is `false` and should stay so).

See `docs/PAPER_TRADING.md`.