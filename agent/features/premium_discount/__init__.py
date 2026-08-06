"""Premium / Discount Zone analysis package.

Deterministic SMC-style premium-discount analysis (no ML, no LLM, no
heuristics):

- ``PremiumDiscountAnalyzer`` (analyzer.py) — pure-algorithm premium/discount
  ratios and zone from the latest confirmed swing range.
- ``PremiumDiscountFeature`` (feature.py) — opt-in Feature Engine wrapper
  (buy in discount, sell in premium).
"""

from agent.features.premium_discount.analyzer import PremiumDiscountAnalyzer
from agent.features.premium_discount.feature import PremiumDiscountFeature

__all__ = [
    "PremiumDiscountAnalyzer",
    "PremiumDiscountFeature",
]
