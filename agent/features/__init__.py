"""Feature Engine package.

Public surface:

- ``build_feature_engine`` — dependency-injected factory for the engine.
- ``FeatureEngine`` / ``FeatureSet`` / ``compute_df_features`` — orchestration.
- ``FeatureResult`` / ``BaseFeature`` — the feature contract.
- ``FEATURE_REGISTRY`` — ordered feature classes.

Typical wiring (see ``agent.bot`` and ``agent.core.screener``)::

    engine = build_feature_engine(market_data=self.exchange, config=config)
    feature_set = engine.compute(symbol, df)          # full analysis
    feature_set = engine.compute(symbol, df, include_market=False)  # df-only
"""

from agent.features.base import BaseFeature, FeatureResult
from agent.features.engine import (
    DF_ONLY_NAMES,
    FEATURE_REGISTRY,
    FeatureEngine,
    FeatureSet,
    compute_df_features,
)
from agent.features.funding import FundingFeature
from agent.features.fvg import FairValueGapAnalyzer, FairValueGapFeature
from agent.features.liquidity import (
    LiquidityFeature,
    SmartMoneyLiquidityAnalyzer,
    SmartMoneyLiquidityFeature,
)
from agent.features.market_structure import (
    MarketStructureAnalyzer,
    MarketStructureFeature,
)
from agent.features.order_block import OrderBlockAnalyzer, OrderBlockFeature
from agent.features.sentiment import SentimentFeature
from agent.features.structure import StructureFeature
from agent.features.trend import TrendFeature
from agent.features.volume import VolumeFeature
from agent.features.volatility import VolatilityFeature
from agent.features.whale import WhaleFeature
from typing import Optional

__all__ = [
    "DF_ONLY_NAMES",
    "FEATURE_REGISTRY",
    "BaseFeature",
    "FeatureEngine",
    "FeatureResult",
    "FeatureSet",
    "FairValueGapAnalyzer",
    "FairValueGapFeature",
    "FundingFeature",
    "LiquidityFeature",
    "MarketStructureAnalyzer",
    "MarketStructureFeature",
    "OrderBlockAnalyzer",
    "OrderBlockFeature",
    "SmartMoneyLiquidityAnalyzer",
    "SmartMoneyLiquidityFeature",
    "SentimentFeature",
    "StructureFeature",
    "TrendFeature",
    "VolumeFeature",
    "VolatilityFeature",
    "WhaleFeature",
    "build_feature_engine",
    "compute_df_features",
]


def build_feature_engine(
    market_data=None,
    config: Optional[dict] = None,
    enabled: Optional[list] = None,
) -> FeatureEngine:
    """Build a FeatureEngine with dependency injection.

    ``market_data``: anything satisfying the ``MarketData`` protocol
    (``ExchangeClient`` in production, a fake in tests).
    ``enabled``: optional explicit feature-name whitelist; defaults to the
    ``features.enabled`` config list or the full registry.
    """
    return FeatureEngine(
        market_data=market_data,
        config=config,
        features=None if enabled is None else _from_registry(enabled, market_data, config),
    )


def _from_registry(enabled: list, market_data, config) -> list:
    wanted = set(enabled)
    return [cls(market_data, config) for cls in FEATURE_REGISTRY if cls.name in wanted]
