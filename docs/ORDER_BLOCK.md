# Order Block Analyzer

Deterministic, non-repainting Smart-Money-Concept (SMC) order-block detection.
Pure algorithm — no ML, no LLM, no candle-color heuristics. Opt-in Feature
Engine feature: name `order_blocks`, `df_only=True`, `opt_in=True`.

## What an order block is

An order block is the **origin candle** of an impulsive price leg. Its body
(zone `[low, high]`) is treated as an unfilled resting-order area that price
tends to revisit.

Algorithm (strictly a pure function of the DataFrame):

1. **Swing points** — fractal swing highs/lows using the shared definition:
   bar `i` is a swing high when `high[i]` strictly exceeds `strength` highs on
   both sides (`strength = pivot_strength`, default 1). Only bars with
   `i + strength < len(df)` are confirmed (non-repainting); trailing bars are
   never emitted.
2. **Legs** — confirmed swing highs and lows alternate. An impulse leg runs
   from a swing extreme to the next confirmed opposite extreme and must exceed
   `impulse_min_atr * ATR(leg end)` (default 1.0) to be considered impulsive.
   Impulse = `abs(high_end - low_end)` of the leg.
3. **Order blocks** — the origin candle at the leg start:
   - Bullish OB at a confirmed **swing low**: zone `[low_origin, high_origin]`.
   - Bearish OB at a confirmed **swing high**: zone `[low_origin, high_origin]`.

Zones use the origin candle's high/low (body edges), **not** wick extremes —
same convention as the FVG analyzer.

## Status

Status is decided by every later bar (strict re-entry — a single touch exactly
at the zone edge does **not** fill it):

| status       | bullish OB condition                                 | bearish OB condition                              |
|--------------|------------------------------------------------------|---------------------------------------------------|
| `unmitigated`| no later close below `low`; no fill                  | no later close above `high`; no fill              |
| `mitigated`  | some later `low < high` (zone entered), close held   | some later `high > low` (zone entered), close held|
| `invalidated`| a later **close** below `low`                        | a later **close** above `high`                    |

Precedence when more than one condition matches: **invalidated > mitigated >
unmitigated** (same convention as the FVG analyzer).

- `filled_ratio`: fraction of the zone consumed by the strongest adverse
  penetration (`1.0` when invalidated).
- **Breaker**: an invalidated block whose zone is later reclaimed by a close
  back **through** it in the original direction (bullish OB closed above
  `high` again, or bearish OB closed below `low` again). Breakers are labeled
  `direction = "breaker"` with `original_direction` preserved. This flip rule
  is price-action only — no candle-color heuristics.

## Quality score

`quality` in `[0, 1]`, ranked best-first (`rank` starts at 1):

| component                     | weight | notes                                      |
|-------------------------------|--------|--------------------------------------------|
| validity                      | 0.40   | breaker 0.30 / unmitigated 0.40 / mitigated 0.20 |
| zone size vs ATR              | 0.15   | closer to ATR is better                    |
| recency                       | 0.25   | decaying with bars since the block         |
| impulse vs 3×ATR              | 0.20   | capped at full credit from 3×ATR           |

`quality_tier`: `high` ≥ 0.60, `medium` ≥ 0.35, `low` otherwise.

## Result schema

Top level: `order_blocks` (list), `count`, `timeframe`, `reason`
(`"ok"`/`"insufficient_data"`/`"analysis_failed"`).

Each block:

```
direction            "bullish" | "bearish" | "breaker"
original_direction   "bullish" | "bearish"
status               "unmitigated" | "mitigated" | "invalidated"
breaker              bool
high, low            zone edges (origin candle body)
index                origin bar index
swing_index          confirmed swing bar index
leg_high, leg_low    leg extremes
leg_direction        "up" | "down"
leg_size             abs(high_end - low_end)
atr                  ATR at leg end (pandas-series value at that bar)
filled_ratio         [0, 1]
quality              [0, 1]
quality_tier         "high" | "medium" | "low"
rank                 1-based best-first
timeframe            timeframe label
```

All numeric fields are Python `float`/`int` (numpy types are cast).

## Feature wrapper

- `signal`: best actionable block — most recent actionable unmitigated block's
  direction; a breaker contributes its **flipped** polarity
  (`_BREAKER_SIGNAL`), otherwise `"neutral"`.
- `confidence`: `0.5 + 0.4 * quality`.
- `metadata`: full result plus `best_actionable`.

## Guarantees

- **Deterministic**: pure function of the df — same df in, identical result out.
- **Non-repainting**: only confirmed swings (`i + strength < len(df)`) drive
  detection; trailing bars are ignored.
- **Fail-open**: `neutral_result` (empty `order_blocks`, `count=0`, reason) on
  insufficient data or unexpected failure; never raises.
- **No candle-color heuristic**: detection depends only on high/low/close —
  flipping a candle's open/close relationship leaves results identical
  (covered by a dedicated test).
- **Opt-in**: registered in `FEATURE_REGISTRY`, absent from the default engine.

## Configuration

Config key `order_block` (fallback `order_block_analyzer`):

| key                | default | meaning                                  |
|--------------------|---------|------------------------------------------|
| `pivot_strength`   | 1       | fractal strength for swing points        |
| `impulse_min_atr`  | 1.0     | impulse threshold, ×ATR at leg end       |
| `atr_period`       | 14      | ATR period                               |
| `max_lookback`     | 400     | bars scanned for blocks                  |
| `max_blocks`       | 8       | max blocks returned                      |
| `min_bars`         | 16      | minimum bars before analysis             |
| `timeframe`        | "15m"   | default timeframe label                  |

## Cross-analyzer conventions

Shares the fractal swing definition, the `invalidated > mitigated >
unmitigated` precedence, `neutral_result` fail-open pattern, multi-timeframe
`analyze_timeframes`, and `min_bars`/insufficient-data semantics with the
Market Structure and FVG analyzers.
