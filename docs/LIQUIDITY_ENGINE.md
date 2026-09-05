# Liquidity Engine — deterministic structure-heavy liquidity analysis

The 32-phase upgrade **reused the existing liquidity architecture** rather than
reinventing it (Rule: no full rewrite). Two distinct analyzers live in
`agent/features/liquidity/`:

| Analyzer | Signal | Consumed by |
|---|---|---|
| `LiquidityFeature` (orderbook) | tradability: spread/depth/imbalance | the live `liquidity` feature |
| `SmartMoneyLiquidityFeature` (smart_money) | structural liquidity: sweeps, stop hunts, EQL/EQH, dealing range | the `smart_money_liquidity` feature + screener |

## How it fits the decision layer

The unified `EvidenceCollector` (`agent/decision/evidence.py`) reads these as
**order_flow / liquidity** votes:

* `smart_money_liquidity` → `liquidity` evidence source (weight 3 — highest)
* `liquidity` (orderbook) → `order_flow` evidence source (weight 2)

Both contribute *signed confidence*; neither can veto alone. This keeps the
feature darkness principle: liquidity informs the score, and only hard vetoes
(which are liquidity-independent) can block outright.

## Full documentation

* `docs/SMART_MONEY_LIQUIDITY.md` — sweeps, pools, dealing range, internal/external
* `docs/ORDER_BLOCK.md`, `docs/FVG.md`, `docs/MARKET_STRUCTURE.md`, `docs/PREMIUM_DISCOUNT.md`

## Deferred / not re-implemented

The speculative "liquidity mining pools" odds-checker (Phase 16) is **not** a
tradable feature and was intentionally deferred — strong-liquidation candles
are instead captured by the existing sweep detection. Confidence in a suite
that is explainable and tested beats cargo-cult pool math.