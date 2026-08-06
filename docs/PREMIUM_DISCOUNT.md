# Premium / Discount Zone Analyzer

Deterministic, non-repainting SMC-style premium/discount analysis. Pure
algorithm — no ML, no LLM, no heuristics. Opt-in Feature Engine feature: name
`premium_discount`, `df_only=True`, `opt_in=True`.

## What it computes

Between the **latest confirmed swing high** and **latest confirmed swing low**,
the range is split by its midpoint — the **equilibrium (EQ)**. The last close's
position is reported as:

```
range    = swing_high - swing_low
eq       = (swing_high + swing_low) / 2
discount = (price - swing_low) / range     # clamped to [0, 1]
premium  = (swing_high - price) / range    # clamped to [0, 1]
```

`premium + discount == 1.0` always. The zone is:

| price position                        | zone          |
|---------------------------------------|---------------|
| below EQ (within `equilibrium_tolerance_pct` of it, else) | `discount` |
| above EQ                              | `premium`     |
| on EQ (within tolerance)              | `equilibrium` |
| analysis degraded                     | `unknown`     |

SMC reading: price in the discount zone is *undervalued* (favor buys), price in
the premium zone is *overvalued* (favor sells).

Example (swing low 10.5, swing high 15.5, last close 11.9):

```
range = 5.0   eq = 13.0
discount = (11.9 - 10.5) / 5.0 = 0.28
premium  = 0.72
zone     = "discount"
```

## Result schema

Success (top level also carries `symbol`, `timeframe`, `reason=None`):

```
premium              float  fraction of range above price (clamped [0,1])
discount             float  fraction of range below-or-equal to price [0,1]
zone                 "discount" | "premium" | "equilibrium"
equilibrium          float  (swing_high + swing_low) / 2
swing_high, swing_low        float  latest confirmed swing prices
swing_high_index, swing_low_index  int  bar indices of those swings
price                float  last close
range                float  swing_high - swing_low
position             float  raw (price - swing_low) / range (unclamped)
in_range             bool   swing_low <= price <= swing_high
```

Neutral (fail-open) keeps the same keys with `zone="unknown"`, `premium=None`,
`discount=None`, plus `reason`:
`"insufficient_data"` (fewer than `min_bars`), `"no_recent_swing_range"` (no
confirmed swing on one side within `lookback`), or `"analysis_failed"`.

## Swing selection and lookback

Uses the shared fractal definition (`find_swings`): bar `i` is a swing high
when `high[i]` strictly exceeds the `strength` highs on both sides, and it is
confirmed only when `i + strength < len(df)` (non-repainting).

`lookback` (default 100) limits which confirmed swings can define the range: the
most recent confirmed swing high and the most recent confirmed swing low **within
the last `lookback` bars**. If either side has none inside the window the
analysis degrades to neutral — no fallback, so the window is a hard constraint.

## Feature wrapper

- `signal`: `"bullish"` in the discount zone, `"bearish"` in the premium zone,
  `"neutral"` on equilibrium/unknown.
- `confidence`: `0.5 + 0.4 * depth`, where `depth` is how far price is from EQ
  as a fraction of the half-range (1.0 at the range edge, 0.0 at EQ).
- `metadata`: the full analyzer result.

## Guarantees

- **Deterministic**: pure function of the df.
- **Non-repainting**: only confirmed swings are ever used.
- **Fail-open**: `neutral_result` on insufficient data, missing swings, or any
  unexpected failure; never raises.
- **Opt-in**: registered in `FEATURE_REGISTRY`, absent from the default engine.

## Configuration

Config key `premium_discount` (fallback `premium_discount_analyzer`):

| key                          | default | meaning                                |
|------------------------------|---------|----------------------------------------|
| `pivot_strength`             | 2       | fractal strength for swing points      |
| `lookback`                   | 100     | bars back to search for the range      |
| `min_bars`                   | 16      | minimum bars before analysis           |
| `equilibrium_tolerance_pct`  | 0.0     | % of EQ price to still count as equilibrium |
| `timeframe`                  | "default" | default timeframe label              |

## Cross-analyzer conventions

Shares the fractal swing definition, `neutral_result` fail-open pattern,
multi-timeframe `analyze_timeframes`, and `min_bars` semantics with the Market
Structure, Order Block, and FVG analyzers.
