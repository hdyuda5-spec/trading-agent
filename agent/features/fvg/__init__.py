"""Fair Value Gap analysis package.

Deterministic SMC-style FVG detection (no ML, no LLM):

- ``FairValueGapAnalyzer`` (analyzer.py) — pure-algorithm detection of
  bullish/bearish FVGs with unmitigated / mitigated / filled / invalidated
  status, configurable minimum gap size, and multi-timeframe analysis.
- ``FairValueGapFeature`` (feature.py) — opt-in Feature Engine wrapper.
"""

from agent.features.fvg.analyzer import FairValueGapAnalyzer
from agent.features.fvg.feature import FairValueGapFeature

__all__ = [
    "FairValueGapAnalyzer",
    "FairValueGapFeature",
]
