# ARCHITECTURE AUDIT — trading-agent

Date: 2026-09-05
Repo: `/home/ubuntu/trading-agent` (Python, ccxt-based USDT-M futures agent)
Baseline: `pytest` — 380 passed in ~5s.

---

## 1. Current architecture

Event-driven asyncio pipeline. The old monolith `TradingBot` was already split
into a service layer; `agent/bot.py` is now an orchestrator only.

```
main.py (entry)
  └─ TradingBot.run()
       └─ asyncio.run(_async_run)
            MarketFeed (WS) ──► EventBus
              │                    ├─ market.candle ─► CandleStore ─► evaluate_symbol
              │                    └─ market.tick  ─► last price cache
            management loop (poll):
              portfolio.manage / guard_sl_tp / cancel_stale_orders / run_grid
              screen.should_run / whale_svc.should_scan / reporting.should_report
```

Signal path:

```
candle closed ─► SignalService.evaluate
  ├─ FeatureEngine.compute(symbol, df)  (trend, volatility, volume, structure,
  │      market_structure, smart_money_liquidity, whale, sentiment, funding,
  │      open_interest, fvg, order_blocks, ...)
  ├─ Strategy.generate_signal  (momentum | ai_signal | support_resistance)
  ├─ TrendFilter (1h EMA/ADX)        ─ hard skip
  ├─ TradeFilters (whale, regime)    ─ hard skip
  ├─ one_position_per_symbol / losing streak / trading hours ─ hard skip
  ├─ RiskEngine (fee tolerance, can_open)               ─ hard skip
  └─ ExecutionService.open_position ─► OrderManager.open_position
       └─ create_order ─► wait_fill ─► place_sl_tp (TP limit reduce-only + SL algo stop)
```

Screener path:

```
ScreenerService.run ─► Screener.candidates (top-N by volume)
  └─ auto_trade_candidates
       ├─ ScreenerScorer.evaluate  (weighted confidence, modifiers)
       ├─ ScreeningGate.check_global / check_order  (5 hard-reject classes)
       └─ ExecutionService.open_position
```

Portfolio/position lifecycle (management loop):

```
portfolio.manage        daily-loss halt, trailing stop, external-close reconcile
portfolio.guard_sl_tp   re-place TP/SL if dfsx
portfolio.close_position  compute PnL ─► TradeStore.record_trade ─► reflector
```

### Module map

| Layer            | Files |
|------------------|-------|
| Entry            | `main.py` |
| Orchestrator     | `agent/bot.py` |
| Exchange         | `agent/core/exchange.py` (ccxt wrapper, algo-stop raw API for Binance) |
| Market data      | `agent/data/market_data.py`, `agent/data/collector.py`, `agent/services/market_feed.py`, `agent/services/candle_store.py` |
| Features         | `agent/features/**` (engine + registry + trend/volatility/volume/structure/market_structure/liquidity/smart_money/whale/sentiment/funding/open_interest/fvg/order_block/premium_discount) |
| Strategies       | `agent/strategies/**` (momentum, mean_reversion, grid, ai_signal, support_resistance) |
| Decision         | `agent/strategies/decision.py` (DecisionEngine weighted vote), `agent/strategies/scoring.py` (ScreenerScorer + ScreeningGate) |
| Risk             | `agent/core/risk.py` (policy composition: sizing/exposure/SL/TP/leverage/drawdown/validation) |
| Execution        | `agent/services/execution.py`, `agent/execution/order.py` |
| Portfolio        | `agent/services/portfolio.py` |
| Data store       | `agent/data/trades.py` (SQLite trades/state/experience), `agent/services/portfolio.py` |
| Notifications    | `agent/execution/notifier.py`, `agent/execution/telegram_ctl.py` |
| Scheduled        | `agent/services/screener_service.py`, `agent/services/whale_service.py`, `agent/services/reporting.py` |
| Misc             | `agent/core/trend.py`, `agent/core/whale.py`, `agent/core/screener.py`, `agent/core/patterns.py`, `agent/core/support_resistance.py`, `agent/core/reflector.py`, `agent/services/event_bus.py`, `agent/services/filters.py`, `agent/services/signals.py` |

---

## 2. Execution flow (order lifecycle today)

```
Signal/Decision
  └─ RiskEngine.can_open (fee tolerance, exposure, positions, drawdown)
  └─ OrderManager.open_position
       ├─ compute_position_size (notional % equity, not risk-based)
       ├─ enforce_leverage
       ├─ create_order (limit postOnly | market) × retry_attempts
       ├─ _wait_fill (poll entry_ttl_seconds)   ─► timeout → cancel_pending
       ├─ place_sl_tp  (TP reduce-only limit + SL closePosition algo stop)
       └─ notifier.send_open
```

State transitions are informal (dict status strings). There is **no explicit
state enum**, no persisted `ticket_id`, no idempotency guarantee, and no
order→position reconciliation journal.

---

## 3. Dependency flow

```
config.json ─► TradingBot ─► services (injected, constructor DI)
exchange (ExchangeClient)  injected into: risk, orders, feature_engine,
         strategies, trend, portfolio, execution, signals, screen, whale_svc
TradeStore (SQLite)  injected into: portfolio, screen, reflector
EventBus  injected into: portfolio, whale_svc; subscribed by bot
No globals / no singleton state machine; DI is clean.
```

Dependency direction is sane; the old bot's attributes are preserved as thin
shims in `agent/bot.py` for `TelegramController`.

---

## 4. Signal flow

Strategies emit `generate_signal()` dicts:
`{side, confidence, price, reason[], risk{sl,tp,rr}, metadata, strategy, action}`.
The `DecisionEngine` (feature pipeline) and `ScreenerScorer` emit verdicts;
`RiskEngine` attaches SL/TP. Both paths funnel into
`ExecutionService.open_position` → `OrderManager.open_position`.

Two parallel decision stacks exist (**technical debt #1**): the cross-section
DecisionEngine (`agent/strategies/decision.py`) and the screener
ScreenerScorer (`agent/strategies/scoring.py`) with duplicated weight tables
and different rejection semantics.

---

## 5. Risk flow

- `RiskEngine.can_open` (validation): daily loss, max open positions, exposure.
- `RiskEngine.check_fee_tolerance`: spread+fee filter.
- Sizing: `PositionSizingPolicy.notional` = % equity × volatility factor
  (fixed-notional mode for small accounts).
- SL/TP: `StopLossPolicy`/`TakeProfitPolicy` ATR-based with % caps.
- Trailing: ATR or % in `trailing_stop_hit`.
- Drawdown: daily baseline persisted; top-up resets baseline.

**MISSING (Phase 8/9):** risk-per-trade sizing
(`size = risk_amount / |entry - stop|`), `RR_TOO_LOW` validation, notional cap
check against `max_position_pct`, and max-leverage interplay with risk sizing.

---

## 6. Database flow

`TradeStore` (SQLite at `data/trades.db`):
- `trades` (ts, symbol, side, strategy, entry, exit, qty, pnl, pnl_pct, reason, confidence)
- `state` (key/value kv for trailing, equity baseline, last_auto_screen)
- `experience` (trade lessons for reflector/LLM)

**MISSING (Phase 21):** order ids, exchange order id, position id, fees,
slippage, entry/sl/tp snapshot, regime, score, evidence, decision; no
`decisions`, `missed_trades`, `tickets`, `telemetry` tables; no safe migration
runner beyond one ad-hoc `ALTER TABLE` for `confidence`.

---

## 7. Problems found

### Bugs (confirmed)

1. **Stale-order timestamp heuristic** (`agent/execution/order.py:284`)
   `(now - ts / 1000.0) if ts > 1e12 else now - ts` — uses a magic 1e12 split.
   Milliseconds near/above the boundary and any microseconds timestamp are
   mis-scaled, risking an order considered hours old that is minutes old (or
   vice-versa). No tests. **P0.**

2. **Missing RR validation before order.** `risk.build_stop_loss`/`build_take_profit`
   produce levels but nothing checks `(tp-entry)/(entry-sl) >= min_rr` before an
   order is created. **P0.**

3. **Stale SL/TP algo (conditional) orders are never cancelled.** The Binance
   raw algo-stop API path (`create_stop_order`) is a separate order universe
   (`/fapi/v1/algoOrder`) that `fetch_open_orders`/`cancel_stale_orders` never
   see. Orphaned stops accumulate after partial fills/reversals. **P1.**

4. **Slippage/fees not captured** in `record_trade`; PnL is marked-to-entry price,
   so `close_position` reports theoretical not realized PnL. **P1.**

5. **No SL/TP placement verification with critical failure semantics.**
   `place_sl_tp` alerts on failure but the caller proceeds to report
   `[OPEN]` success; a failed stop leaves the position claimed "protected".
   Violates Phase 15 ("do not pretend"). **P1.**

### Latent issues / robustness

6. **Duplicate-order window.** Between strategy→risk→order there is no check
   that an existing *open entry order* for the symbol exists; two paths
   (strategy + screener) can race on the same symbol. `one_position_per_symbol`
   covers positions, not pending entries. **P1.**

7. **`cancel_stale_orders` swallows `Exception`** in the service layer
   (silent failure — violates "no silent failure" rule). **P2.**

8. **Market regime missing.** Only a whale net-flow regime gate exists in
   `TradeFilters.market_regime_allows`. No volatility/trend-structure based
   regime (Phase 11). **P1.**

9. **Retries can duplicate a limit order if the first actually filled** between
   `_wait_fill` timeout and `cancel_pending` (window is small but real). **P1.**

10. **`equity()` fragility**: with a zeroed Binance futures account, ccxt omits
    top-level USDT entry so `fetch_balance().get("USDT")` is `None` and equity
    reads 0. Correct for an empty account, but the parser should also read
    `info.assets` so a funded-but-unparsed balance is never misreported. **P2.**

---

## 8. Technical debt

1. **Two decision stacks** (DecisionEngine + ScreenerScorer) with duplicated
   weights and rejection semantics.
2. **Weights hardcoded** in `decision.py` (DEFAULT_WEIGHTS) and `scoring.py`
   (WEIGHTS) with partial config override; no single `decision.weights` source
   of truth (Phase 5).
3. **Untracked/partial migration state** in git: several services and tests
   are uncommitted; `ecosystem.config.js` untracked.
4. **No config validation / fail-fast** — a typo in `config.json` silently
   becomes a default or a runtime error mid-cycle (Phase 22).
5. **Magic numbers** scattered (1800s reconcile window, 1e12 timestamp, fee 0.02,
   position TTL 180/300) — not centralized.
6. **Backward-compat shims** on `TradingBot` grow with every consumer; consider a
   proper CLI/inspection layer.
7. **No paper-trading broker** — the only safe mode is testnet API flags.
8. **No NEWS provider abstraction** (`NewsProvider`/`SentimentProvider`);
   ai_signal hardcodes an OpenRouter style URL; screener reads `news` list.

---

## 9. Priority recommendations

### P0 (correctness/safety, do now)
- [ ] Set `screener.auto_trade = false` (currently true).
- [ ] Central timestamp util; fix `cancel_stale_orders`; unit tests ms/s/µs, stale/fresh.
- [ ] RR validation (`RR_TOO_LOW`) before order creation.
- [ ] Risk-per-trade sizing (`risk_amount = equity×risk_pct`, size = risk/SL-dist) with caps.
- [ ] Unified `TradeTicket` + idempotency (ticket_id) and ticket→order linkage.
- [ ] Rejection telemetry + Signal Funnel; explainable `reason_code` on every reject.
- [ ] Missed-trade journaling (Phase 19).
- [ ] TradeStore schema v2 with safe migrations (Phase 21).
- [ ] Config validation, fail-fast at startup (Phase 22).
- [ ] Tests for all of the above + quality gate (`pytest`).
- [ ] Docs (audit/architecture/decision/risk/execution/liquidity/paper/ops).

### P1 (safety/observability)
- [ ] Market Regime Engine (Phase 11).
- [ ] Liquidity engine: explicit interface + safe fallback + logging for data gaps (Phase 10).
- [ ] SL/TP placement verification + critical log when unprotected (Phase 15).
- [ ] Cancel stale algo stops (Phase 1/2 extension).
- [ ] Trade Reviewer + classification (WIN/LOSS/BREAKEVEN/MISSED_ENTRY/...) (Phase 18).
- [ ] Cycle observability summary (Phase 20).
- [ ] Paper trading broker (Phase 25).
- [ ] Fees/slippage capture in trade records.

### P2
- [ ] NewsProvider/SentimentProvider abstraction (Phase 17).
- [ ] Cache dedup for OHLCV/orderbook requests; rate-limit audit (Phase 27).
- [ ] Restart reconciliation hardening (exchange-authoritative) (Phase 28).
- [ ] `equity()` reading `info.assets`.

### P3
- [ ] Multi-agent desk orchestration, Docker, deeper backtest tooling (Phase 24).

---

## Compass (per master prompt)

Priority: **Correctness > Safety > Risk Control > Observability > Performance > Feature count.**
AI/advisory stays optional and non-vetoing; live trading stays OFF
(`auto_trade=false`, no real key exposure, paper/testnet first).