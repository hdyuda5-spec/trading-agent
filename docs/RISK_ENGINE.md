# Risk Engine — policy composition, risk-based sizing, RR validation

`agent/core/risk.py` hosts the **RiskEngine** (`agent.services` composites on
top). It is a policy engine: each numbered policy is a registered, independent
check; enabling/disabling is purely config.

## Policies (composed in order)

| # | Policy | Guards |
|---|---|---|
| 1 | `DailyLossPolicy` | daily loss limit → halt |
| 2 | `MaxOpenPositionsPolicy` | global position cap |
| 3 | `ExposurePolicy` | max total exposure % |
| 4 | `MinEquityPolicy` | stop below `min_equity_usdt` |
| 5 | `MinFeeTolerancePolicy` | spread/fee tolerance |
| 6 | `MaxSizePolicy` | single-position size cap |
| 7 | `PositionSizingPolicy` | step rounding + fixed-notional vs risk-based |
| 8 | `RiskBasedSizingPolicy` | qty = `risk_amount / |entry−SL|`, capped, stepped |
| 9 | `RRValidationPolicy` | SL/TP ordering + `min_rr` (default 1.5) |

## New in this upgrade

* **`RiskBasedSizingPolicy`** — sizes by *risk-per-trade* instead of notional:
  `risk_amount = equity × risk_per_trade_pct/100`, then
  `qty = risk_amount / |entry − SL|`, capped by `max_position_pct` and stepped
  to market precision. One bad trade costs at most `risk_per_trade_pct` of
  equity (when the SL is respected).
* **`RRValidationPolicy`** — `validate_rr(entry, sl, tp, side)` returns
  `OK / RR_TOO_LOW / INVALID_LEVEL / INVALID_SL` so a strategy can never emit a
  take-profit that is worse than the stop.
* RiskEngine now also delegates `risk_per_trade_amount`, `risk_position_size`,
  and `validate_rr` — the only sizing API the decision layer uses.

## Config

```jsonc
"risk": {
  "risk_per_trade_pct": 1.0,      // risk-based sizing risk per trade
  "max_position_pct": 10,
  "min_rr": 1.5,                  // RRValidationPolicy
  "daily_loss_limit_pct": 5.0,
  "min_equity_usdt": 20,
  "max_open_positions": 2,
  "max_total_exposure_pct": 90,
  "leverage": 5, "max_leverage": 5
}
```

Every public gate (`can_open`, `compute_position_size`, `validate_rr`, …) keeps
its legacy contract (backward compatible).

## Tests

* `tests/test_risk.py` — engine composition + reduced-policy behavior
* `tests/test_risk_v2.py` — new sizing/RR policies (13 tests)