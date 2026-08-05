# Fair Value Gap (FVG) Detector

Deterministic SMC-style fair value gap detection. Pure algorithm — no
machine learning, no LLM, no subjective drawings. Every output is a fixed
function of the input OHLCV DataFrame.

## Module

`agent/features/fvg/`

- `analyzer.py` — `FairValueGapAnalyzer` (core, df-only)
- `feature.py` — `FairValueGapFeature` (BaseFeature wrapper, opt-in)
- `__init__.py` — package re-exports

## Detection rules (assumptions)

A fair value gap is the price imbalance between candle `i-1` and candle
`i+1`, separated by the impulse candle `i`:

- **Bullish FVG** — `low[i+1] > high[i-1]`; zone `[high[i-1], low[i+1]]`.
- **Bearish FVG** — `high[i+1] < low[i-1]`; zone `[high[i+1], low[i-1]]`.

Gap status is evaluated against every bar after the gap (from `i+2` on),
oldest to newest, with the following precedence:

| Status | Bullish FVG condition | Bearish FVG condition |
| --- | --- | --- |
| `invalidated` | a later bar *closed* below the zone low | a later bar *closed* above the zone high |
| `filled` | a later bar's low reached the zone low (full traverse) | a later bar's high reached the zone high |
| `mitigated` | a later bar's low entered the zone (partial fill) | a later bar's high entered the zone |
| `unmitigated` | no re-entry | no re-entry |

`filled` (boolean) is `True` for `filled` and `invalidated` gaps.
`filled_ratio` is the fraction of the zone traversed (0.0 unmitigated, 1.0
filled/invalidated).

Invalidation subsumes a full fill: a bullish FVG whose later close is below
the zone low has been both fully traversed and negated.

### Minimum gap size

`min_gap_pct` filters out micro-gaps. A gap is kept only when

```
gap >= midpoint_price * min_gap_pct / 100
```

Default is `0.0` (keep every gap); set e.g. `0.5` to ignore gaps smaller
than 0.5% of the zone midpoint.

## Output schema

```json
{
  "symbol": "BTC/USDT",
  "timeframe": "15m",
  "fvg": [
    {
      "direction": "bullish",
      "high": 11.5,
      "low": 10.5,
      "filled": false,
      "status": "unmitigated",
      "filled_ratio": 0.0,
      "gap": 1.0,
      "index": 3,
      "timeframe": "15m"
    }
  ],
  "count": 1,
  "reason": null
}
```

The four required keys from the spec — `direction`, `high`, `low`,
`filled` — are present on every gap. Gaps are returned most-recent-last and
capped at `max_gaps`.

## Multi-timeframe

The analyzer runs the identical deterministic detection over several
dataframes and labels every gap with its timeframe:

```python
from agent.features.fvg import FairValueGapAnalyzer
a = FairValueGapAnalyzer({"fvg": {"timeframe": "default"}})
result = a.analyze_timeframes("BTC/USDT", {"5m": df5m, "1h": df1h})
# result == {"5m": {...}, "1h": {...}} — each entry is an analyze() result
```

The Feature Engine wrapper is single-dataframe (`df_only`); multi-timeframe
analysis is available directly on the analyzer.

### Fail-open behavior

`analyze` never raises. Insufficient data (`len < min_bars`, default 5)
returns `reason: "insufficient_data"`; any internal error returns
`reason: "analysis_failed"`. Both return `fvg: []` / `count: 0`.

## Configuration

Analyzer config key: `fvg` (legacy alias: `fair_value_gap`).

| Key | Default | Meaning |
| --- | --- | --- |
| `min_gap_pct` | 0.0 | Minimum gap size as % of zone midpoint price |
| `min_bars` | 5 | Minimum bars before analysis runs |
| `max_gaps` | 100 | Cap on returned gaps (most recent kept) |
| `impulse_lookback` | 6 | Bars back a gap must be to drive the feature signal |
| `timeframe` | "default" | Timeframe label for single-TF analysis |

## Feature Engine integration

`FairValueGapFeature` is **opt-in** (`opt_in = True`): registered in the
unified `FEATURE_REGISTRY` but NOT in the default engine (which still runs
the original 8 features). Enable it explicitly:

```python
from agent.features import build_feature_engine
engine = build_feature_engine(market_data=md, config=cfg, enabled=["fvg"])
```

or via `config.json`:

```json
{ "features": { "enabled": ["trend", "fvg"] } }
```

The feature is `df_only` (no network calls). Its signal is the direction of
the most recent *unmitigated* FVG formed within `impulse_lookback` bars of
the latest bar (bullish/bearish), else `neutral`. Confidence scales with gap
size (0.60–0.90). The full FVG list is exposed in `metadata`
(including `latest_unmitigated`).

## Determinism

All numeric values are cast to Python `float`/`int` (numpy scalar types are
unhashable/incomparable for equality). The analyzer is a pure function of the
DataFrame: `analyze(s, df)` twice returns identical dicts.

## Tests

`tests/test_features/test_fvg.py` — required schema, bullish + bearish FVGs,
all four statuses (both directions), `filled_ratio`, configurable minimum gap
size, multi-timeframe analysis, determinism, insufficient-data neutral,
wrapper + opt-in registry.
