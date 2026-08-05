# Smart Money Liquidity Analyzer

Deterministic, non-repainting structural liquidity analysis. No subjective
ICT drawings — every output is a pure function of the input OHLCV DataFrame.

## Module

`agent/features/liquidity/`

- `smart_money.py` — `SmartMoneyLiquidityAnalyzer` (core, df-only)
- `feature.py` — `SmartMoneyLiquidityFeature` (BaseFeature wrapper)
- `orderbook.py` — original order-book `LiquidityFeature` (moved unchanged)
- `__init__.py` — package re-exports (backward compatible)

## Detection rules (assumptions)

All pivots use fractal definition with `pivot_strength = 2` by default:
bar `i` is a swing high when `high[i]` strictly exceeds the 2 highs before
and after it (mirror for swing lows).

### Non-repainting guarantee

A pivot is *confirmed* only when the full right confirmation window exists
(`i + strength < len(df)`). Pivots in the trailing `strength` bars are
reported as `confirmed=False` candidates and never used to build levels.
Because confirmation requires bars that do not yet exist, historical output
can never change when more data arrives.

### Equal highs / equal lows

Confirmed swing levels are clustered in ascending price order. A level joins
the first cluster whose anchor is within `equal_tolerance_pct` (default
0.15%) of it. Cluster price = mean of members, `touches` = member count.
`touches >= 2` → `equal_high`/`equal_low` source, otherwise
`swing_high`/`swing_low`.

### Buy / sell-side liquidity

- Buy-side liquidity pools (resting bids + short-stops) sit at swing highs
  and equal highs.
- Sell-side pools (resting offers + long-stops) sit at swing lows and equal
  lows.

Each level carries `{price, side, source, touches, index, location}`.

### Dealing range, internal vs external

The dealing range is the most recent confirmed swing high and swing low
within `range_window` (default 30) bars of the latest bar. Buy-side levels
above the range high and sell-side levels below the range low are
`external`; all other levels are `internal`.

### Liquidity sweep / stop hunt

A sweep is only evaluated on the latest bar, against confirmed levels whose
index is within `sweep_lookback` (default 6) bars. It requires both a wick
pierce and a body rejection (close back through the level):

- Bullish: `low < level < close` with the low below the body → liquidity
  taken below a sell-side pool, price reclaimed above it.
- Bearish: `high > level > close` with the high above the body → liquidity
  taken above a buy-side pool, price reclaimed below it.

A sweep on a level with `touches >= 2` (a demonstrated stop cluster) is
additionally flagged `stop_hunt: True`.

### Confidence

Deterministic score in 0.50–0.97:

| Component | Contribution |
| --- | --- |
| Base | 0.55 |
| Pierce depth vs ATR | up to +0.12 |
| Stop hunt (equal level) | +0.10 |
| External liquidity level | +0.06 |
| Body rejection >= 0.5 ATR | +0.06 |
| Volume ratio >= 1.2 vs 20-bar mean | +0.08 |

## Output schema

```json
{
  "symbol": "BTC/USDT",
  "swing_highs": [{"index": 9, "price": 13.5, "confirmed": true}],
  "swing_lows": [...],
  "equal_highs": [...],
  "equal_lows": [...],
  "buy_side_liquidity": [{"price": 12.5, "side": "buy", "source": "swing_high", "touches": 1, "index": 9, "location": "internal"}],
  "sell_side_liquidity": [...],
  "internal_liquidity": [...],
  "external_liquidity": [...],
  "dealing_range": {"high": 12.5, "high_index": 9, "low": 8.5, "low_index": 5},
  "liquidity_sweep": true,
  "sweep_direction": "bullish",
  "stop_hunt": false,
  "sweep": {"direction": "bullish", "level": {...}, "bar_index": 17, "pierce": 2.0, "stop_hunt": false},
  "confidence": 0.649,
  "reason": null
}
```

### Fail-open behavior

`analyze` never raises. Insufficient data (`len < min_bars`, default 16)
returns `reason: "insufficient_data"`; any internal error returns
`reason: "analysis_failed"`. Both are neutral results (`liquidity_sweep: false`,
`confidence: 0.5`) so downstream signal aggregation degrades gracefully.

## Configuration

Analyzer config key: `smart_money_liquidity` (legacy alias:
`liquidity_analyzer`).

| Key | Default | Meaning |
| --- | --- | --- |
| `pivot_strength` | 2 | Fractal confirmation width |
| `equal_tolerance_pct` | 0.15 | Equal-level clustering tolerance (%) |
| `range_window` | 30 | Bars back for dealing-range definition |
| `liquidity_lookback` | 60 | Drop levels older than this from the latest bar |
| `sweep_lookback` | 6 | Max bars between a level and a valid sweep |
| `atr_period` | 14 | ATR period for confidence scoring |
| `min_bars` | 16 | Minimum bars before analysis runs |
| `volume_ratio_window` | 20 | Bars for the volume-ratio confidence boost |

## Feature Engine integration

`SmartMoneyLiquidityFeature` is **opt-in** (`opt_in = True`): it is registered
in the unified `FEATURE_REGISTRY` but is NOT built into the default engine
(which runs the original 8 features). Enable it explicitly:

```python
from agent.features import build_feature_engine
engine = build_feature_engine(market_data=md, config=cfg, enabled=["smart_money_liquidity"])
```

or via `config.json`:

```json
{ "features": { "enabled": ["trend", "smart_money_liquidity"] } }
```

The feature is `df_only` (no network calls). Its signal is the sweep
direction (`bullish`/`bearish`) or `neutral`; the full analysis dict is
available in `metadata` (without `symbol`).

## Determinism

All numeric values are cast to Python `float`/`int` (numpy scalar types are
unhashable/incomparable for equality). The analyzer is a pure function of the
DataFrame: `analyze(s, df)` twice returns identical dicts.

## Tests

`tests/test_features/test_smart_money_liquidity.py` — schema, swings,
confirmation window, equal levels, liquidity pools, internal/external,
both sweep directions, stop hunt, no-sweep rejection, determinism,
non-repainting, insufficient-data neutral, wrapper + opt-in registry.
