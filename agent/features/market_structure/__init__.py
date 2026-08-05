"""Market Structure analysis package.

Deterministic SMC-style structure analyzer (no ML, no LLM):

- ``MarketStructureAnalyzer`` (analyzer.py) — pure-algorithm detection of
  swing trend, Break of Structure (BOS), Change of Character (CHOCH),
  Market Structure Shift (MSS), plus internal/external structure
  classification against the current dealing range.
- ``MarketStructureFeature`` (feature.py) — opt-in Feature Engine wrapper.
"""

from agent.features.market_structure.analyzer import MarketStructureAnalyzer
from agent.features.market_structure.feature import MarketStructureFeature

__all__ = [
    "MarketStructureAnalyzer",
    "MarketStructureFeature",
]
