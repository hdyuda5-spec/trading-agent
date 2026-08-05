"""Order Block analysis package.

Deterministic SMC-style order block detection (no ML, no LLM, no candle-color
heuristics):

- ``OrderBlockAnalyzer`` (analyzer.py) — pure-algorithm detection of
  bullish/bearish order blocks, breaker blocks, mitigated/invalid status,
  and deterministic quality ranking.
- ``OrderBlockFeature`` (feature.py) — opt-in Feature Engine wrapper.
"""

from agent.features.order_block.analyzer import OrderBlockAnalyzer
from agent.features.order_block.feature import OrderBlockFeature

__all__ = [
    "OrderBlockAnalyzer",
    "OrderBlockFeature",
]
