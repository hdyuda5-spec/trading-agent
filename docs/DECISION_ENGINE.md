# Decision Engine — explainable, auditable trade decisions

The decision layer (`agent/decision/`) turns features + strategy signals into a
structured, explainable **verdict** and — whenever the verdict allows — a risk-
checked **TradeTicket**. It never touches the exchange and never places orders;
execution is the only layer allowed to act on a ticket.

```
features + strategy signal + advisory + news
   │
   ├─ HardVeto        → REJECT (invalid price / SL-TP, daily loss, min equity, halt, exchange)
   ├─ Regime          → TRENDING_UP/DOWN, RANGING, HIGH/LOW_VOLATILITY, UNCERTAIN
   ├─ Evidence        → weighted soft votes per source (score, never hard block)
   └─ Decision        → PASS / WAIT / REJECT  +  reason_code  +  TradeTicket
```

## Modules

| File | Responsibility |
|---|---|
| `types.py` | `EvidenceItem`, `Rejection`, `DecisionVerdict` (serialisable, `to_decision_shape()`) |
| `weights.py` | Single source of truth for weights/thresholds; reads only `config["decision"]` |
| `regime.py` | `MarketRegimeEngine.detect(df, atr)` — regime + `size_multiplier` |
| `evidence.py` | `HardVeto` (hard blocks) + `EvidenceCollector` (soft votes) |
| `engine.py` | `UnifiedDecisionEngine.assess(...)` — the full pipeline |
| `ticket.py` | `TradeTicket` — the only object execution may act on |
| `telemetry.py` | `SignalFunnel` — stage counts + `reason_code` rejection histogram |
| `reviewer.py` | `TradeReviewer` (WIN/LOSS/MAE/MFE journal) + `MissedTradeJournal` |

## Verdict semantics

* **PASS** — soft score above threshold and no hard veto. Downstream may trade.
* **WAIT** — no clear side or below `min_margin`. Never a block, but the score
  is recorded (prevents second-guessing later: it's in the `decisions` table).
* **REJECT** — a hard veto (`INVALID_PRICE`, `INVALID_SL`, `NO_EQUITY`,
  `DAILY_LOSS_LIMIT`, `TRADING_HALTED`, `EXCHANGE_UNAVAILABLE`) or a soft
  `SCORE_TOO_LOW`. Every rejection carries a `reason_code`.

```python
verdict = engine.assess(symbol, df, features=fs, strategy_side="LONG",
                        equity=eq, price=px, sl=sl, tp=tp, atr=atr)
if verdict.should_block():                          # REJECT only
    funnel.reject(verdict.reason_code)
    missed_journal.record(symbol, side, strat, verdict.reason_code, ...)
    return
ticket = TradeTicket.from_verdict(verdict, "momentum", position_size=size)
```

## Weights & thresholds (config-driven)

Everything is under `config["decision"]`; there are **no hardcoded tuning
numbers** outside defaults:

```jsonc
"decision": {
  "min_margin": 0.15,          // |margin| required to leave WAIT
  "thresholds": { "long": 0.0, "short": 0.0 },   // raw-score pass threshold
  "ticket_ttl_seconds": 300,
  "regime": { "high_vol_atr_pct": 2.0, "adx_min": 20 },
  "weights": { "technical": 2, "trend": 2, "momentum": 1, "liquidity": 3,
               "order_flow": 2, "whale": 1, "pattern": 1, "ai": 2,
               "news": 1, "ai_advisory": 0.20 }  // ai_advisory aliases to ai
}
```

## Safety design

* Default thresholds are **lenient (0.0)** so the engine is informational until
  you tune it — but **hard vetoes always win** over a strategy signal.
* `auto_trade` stays `false`; nothing trades because of this layer alone.
* Every verdict + ticket is persisted into the `TradeStore` v2 tables
  (`decisions`, `tickets`, `missed_trades`, `telemetry`).

## Telemetry

`SignalFunnel` counts `strategy_signals → passed_gate → tickets_created →
fills → cancelled/expired` plus the rejection histogram. See
`TelegramController` `/funnel`, `/reject`, `/decisions`, `/missed`.