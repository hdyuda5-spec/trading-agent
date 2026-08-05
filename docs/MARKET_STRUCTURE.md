# Market Structure Analyzer

Deterministic, non-repainting SMC-style structure analysis. Pure algorithm —
no machine learning, no LLM, no subjective drawings. Every output is a fixed
function of the input OHLCV DataFrame.

## Module

`agent/features/market_structure/`

- `analyzer.py` — `MarketStructureAnalyzer` (core, df-only)
- `feature.py` — `MarketStructureFeature` (BaseFeature wrapper, opt-in)
- `__init__.py` — package re-exports

## Detection rules (assumptions)

All pivots are fractal, with `pivot_strength = 2` by default: bar `i` is a
swing high when `high[i]` strictly exceeds the 2 highs before and after it
(mirror for swing lows).

### Non-repainting guarantee

A pivot is *confirmed* only when the full right confirmation window exists
(`i + strength < len(df)`). Trailing pivots are reported as
`confirmed=False` candidates and never used as structure. Because the whole
right window must already be visible, historical output can never change as
more bars arrive.

### Swing Trend

Classified from the two most recent confirmed swing highs and lows within
`trend_window` (default 12) bars:

- higher highs with non-lower lows → `bullish`
- lower lows with non-higher highs → `bearish`
- otherwise, the close slope over `trend_window` bars decides; if still
  ambiguous → `neutral`

### BOS (Break of Structure)

A *state*, in the direction of the swing trend (also valid in a neutral
trend). Bullish BOS is active when price has traded above the most recent
confirmed swing high since that swing formed; bearish BOS below the most
recent confirmed swing low. The state resets when a new swing confirms and
becomes the latest level.

### CHOCH (Change of Character)

The first counter-trend crack (a trend is required):

- bullish trend + price traded below the most recent confirmed swing low →
  bearish CHOCH
- bearish trend + price traded above the most recent confirmed swing high →
  bullish CHOCH

### MSS (Market Structure Shift)

Stronger form of CHOCH: the *close* (body) has traded beyond the
counter-trend level, not just the wick. MSS implies CHOCH.

### Internal / External Structure

The dealing range is the most recent confirmed swing high and low inside
`range_window` (default 30) bars. Swing highs above the range high and swing
lows below the range low are `external`; everything else inside the range is
`internal`.

### Confidence

Deterministic score in 0.50–0.97 for the strongest active event
(MSS > CHOCH > BOS):

| Component | Contribution |
| --- | --- |
| Base: MSS / CHOCH / BOS | 0.62 / 0.56 / 0.55 |
| Pierce depth vs ATR | up to +0.12 |
| MSS (body close beyond structure) | +0.10 |
| Level external to dealing range | +0.06 |
| Volume ratio >= 1.2 vs 20-bar mean | +0.08 |

## Output schema

```json
{
  "symbol": "BTC/USDT",
  "trend": "bullish",
  "bos": true,
  "bos_direction": "bullish",
  "bos_level": {"index": 9, "price": 13.5, "side": "high", "location": "internal"},
  "choch": false,
  "choch_direction": null,
  "choch_level": null,
  "mss": false,
  "mss_direction": null,
  "mss_level": null,
  "swing_high": [{"index": 9, "price": 13.5, "confirmed": true}],
  "swing_low": [{"index": 5, "price": 8.5, "confirmed": true}],
  "internal_structure": [{"index": 9, "price": 13.5, "side": "high", "location": "internal"}],
  "external_structure": [],
  "dealing_range": {"high": 13.5, "high_index": 9, "low": 7.5, "low_index": 14},
  "confidence": 0.649,
  "reason": null
}
```

The six required keys from the spec — `trend`, `bos`, `choch`, `mss`,
`swing_high`, `swing_low` — are always present.

### Fail-open behavior

`analyze` never raises. Insufficient data (`len < min_bars`, default 16)
returns `reason: "insufficient_data"`; any internal error returns
`reason: "analysis_failed"`. Both are neutral (`bos/choch/mss: false`,
`trend: "neutral"`, `confidence: 0.5`).

## Configuration

Analyzer config key: `market_structure` (legacy alias: `structure_analyzer`).

| Key | Default | Meaning |
| --- | --- | --- |
| `pivot_strength` | 2 | Fractal confirmation width |
| `structure_lookback` | 60 | Bars back a swing must be to count as structure |
| `range_window` | 30 | Bars back for dealing-range / internal-external split |
| `trend_window` | 12 | Bars back for swing-trend classification |
| `atr_period` | 14 | ATR period for confidence scoring |
| `min_bars` | 16 | Minimum bars before analysis runs |
| `volume_ratio_window` | 20 | Bars for the volume-ratio confidence boost |

## Feature Engine integration

`MarketStructureFeature` is **opt-in** (`opt_in = True`): registered in the
unified `FEATURE_REGISTRY` but NOT in the default engine (which still runs
the original 8 features). Enable it explicitly:

```python
from agent.features import build_feature_engine
engine = build_feature_engine(market_data=md, config=cfg, enabled=["market_structure"])
```

or via `config.json`:

```json
{ "features": { "enabled": ["trend", "market_structure"] } }
```

The feature is `df_only` (no network calls). Its signal follows the swing
trend and flips to the new direction on a confirmed shift: MSS direction >
CHOCH direction > BOS direction > trend. The full analysis dict (without
`symbol`) is available in `metadata`.

## Determinism

All numeric values are cast to Python `float`/`int` (numpy scalar types are
unhashable/incomparable for equality). The analyzer is a pure function of the
DataFrame: `analyze(s, df)` twice returns identical dicts.

## Tests

`tests/test_features/test_market_structure.py` — required schema, swing
detection + confirmation window, swing-trend classification, BOS (both
directions), CHOCH and MSS (both directions, MSS implies CHOCH),
internal/external classification, determinism, non-repainting,
insufficient-data neutral, wrapper + opt-in registry.
