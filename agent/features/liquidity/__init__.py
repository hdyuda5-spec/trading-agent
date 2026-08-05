"""Liquidity analysis package.

Two distinct analyzers live here:

- ``LiquidityFeature`` (orderbook.py) — the order-book tradability signal
  (spread/depth/imbalance) consumed by the Feature Engine.
- ``SmartMoneyLiquidityAnalyzer`` (smart_money.py) — a deterministic,
  non-repainting structural liquidity analyzer: swing highs/lows, equal
  highs/lows, buy/sell-side liquidity pools, liquidity sweeps and stop hunts,
  plus internal/external classification against the current dealing range.

The ``LiquidityFeature`` import path is unchanged (backward compatible):
``from agent.features.liquidity import LiquidityFeature`` still works.
"""

from agent.features.liquidity.feature import SmartMoneyLiquidityFeature
from agent.features.liquidity.orderbook import LiquidityFeature
from agent.features.liquidity.smart_money import SmartMoneyLiquidityAnalyzer

__all__ = [
    "LiquidityFeature",
    "SmartMoneyLiquidityAnalyzer",
    "SmartMoneyLiquidityFeature",
]
