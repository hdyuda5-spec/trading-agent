"""Feature Engine: orchestrates features into an immutable FeatureSet.

The engine is the only place that knows how to assemble features. It:

- builds the configured feature list (constructor/DI),
- caches market-data features with a per-feature TTL so the tick loop does
  not hammer the exchange,
- never raises: any failing feature degrades to a neutral ``FeatureResult``.

``compute(include_market=False)`` returns a FeatureSet built only from the
candles (trend/volatility/volume/structure) — the zero-API-call path used by
the live tick loop. ``include_market=True`` additionally evaluates
liquidity/whale/sentiment/funding (screener path).
"""

import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

from agent.features.base import BaseFeature, FeatureResult
from agent.features.funding import FundingFeature
from agent.features.fvg import FairValueGapFeature
from agent.features.liquidity import LiquidityFeature, SmartMoneyLiquidityFeature
from agent.features.market_structure import MarketStructureFeature
from agent.features.order_block import OrderBlockFeature
from agent.features.premium_discount import PremiumDiscountFeature
from agent.features.sentiment import SentimentFeature
from agent.features.structure import StructureFeature
from agent.features.trend import TrendFeature
from agent.features.volume import VolumeFeature
from agent.features.volatility import VolatilityFeature
from agent.features.whale import WhaleFeature

FEATURE_REGISTRY = (
    TrendFeature,
    VolatilityFeature,
    VolumeFeature,
    LiquidityFeature,
    StructureFeature,
    WhaleFeature,
    SentimentFeature,
    FundingFeature,
    SmartMoneyLiquidityFeature,
    MarketStructureFeature,
    FairValueGapFeature,
    OrderBlockFeature,
    PremiumDiscountFeature,
)

DF_ONLY_NAMES = frozenset(f.name for f in (TrendFeature, VolatilityFeature, VolumeFeature, StructureFeature))
DEFAULT_FEATURE_NAMES = frozenset(f.name for f in FEATURE_REGISTRY if not f.opt_in)


@dataclass(frozen=True)
class FeatureSet:
    """Immutable bundle of per-symbol feature results."""

    symbol: str
    features: Mapping[str, FeatureResult] = field(default_factory=dict)
    computed_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))

    def __getitem__(self, name: str) -> FeatureResult:
        return self.features[name]

    def __contains__(self, name: str) -> bool:
        return name in self.features

    def get(self, name: str, default: Optional[FeatureResult] = None) -> Optional[FeatureResult]:
        return self.features.get(name, default)

    def names(self) -> tuple:
        return tuple(sorted(self.features))

    def to_dict(self) -> dict:
        return {name: feature.to_dict() for name, feature in self.features.items()}

    def snapshot(self) -> dict:
        """Compact ``{feature_name: signal}`` map, handy for LLM prompts."""
        return {name: feature.signal for name, feature in self.features.items()}

    def _safe(self, name: str) -> FeatureResult:
        return self.features.get(name, FeatureResult.neutral(name))

    @property
    def trend(self) -> FeatureResult:
        return self._safe("trend")

    @property
    def volatility(self) -> FeatureResult:
        return self._safe("volatility")

    @property
    def volume(self) -> FeatureResult:
        return self._safe("volume")

    @property
    def liquidity(self) -> FeatureResult:
        return self._safe("liquidity")

    @property
    def structure(self) -> FeatureResult:
        return self._safe("structure")

    @property
    def whale(self) -> FeatureResult:
        return self._safe("whale")

    @property
    def sentiment(self) -> FeatureResult:
        return self._safe("sentiment")

    @property
    def funding(self) -> FeatureResult:
        return self._safe("funding")


class FeatureEngine:
    def __init__(
        self,
        market_data: Optional[Any] = None,
        config: Optional[dict] = None,
        features: Optional[Sequence[BaseFeature]] = None,
    ) -> None:
        self.market_data = market_data
        self.config = config or {}
        self.features = list(features) if features is not None else self._default_features()
        self._index = {f.name: f for f in self.features}
        self._cache: dict = {}

    def _default_features(self) -> list:
        fcfg = self.config.get("features", {})
        enabled = fcfg.get("enabled") or None
        if enabled:
            wanted = set(enabled)
            return [
                cls(self.market_data, self.config)
                for cls in FEATURE_REGISTRY
                if cls.name in wanted
            ]
        return [
            cls(self.market_data, self.config)
            for cls in FEATURE_REGISTRY
            if cls.name in DEFAULT_FEATURE_NAMES
        ]

    def has_feature(self, name: str) -> bool:
        return name in self._index

    def compute(self, symbol: str, df: Any, include_market: bool = True, now: Optional[float] = None) -> FeatureSet:
        now = now if now is not None else time.time()
        results: dict = {}
        for feature in self.features:
            name = feature.name
            if not include_market and not feature.df_only:
                results[name] = feature.unavailable()
                continue
            cached = self._cache.get((symbol, name))
            if cached is not None and feature.cache_ttl > 0 and now - cached[0] < feature.cache_ttl:
                results[name] = cached[1]
                continue
            try:
                result = feature.compute(symbol, df)
            except Exception:
                result = feature.unavailable("feature computation failed")
            results[name] = result
            if feature.cache_ttl > 0:
                self._cache[(symbol, name)] = (now, result)
        return FeatureSet(symbol=symbol, features=results, computed_at=now)

    def clear_cache(self) -> None:
        self._cache.clear()


def compute_df_features(symbol: str, df: Any, config: Optional[dict] = None) -> FeatureSet:
    """df-only FeatureSet with no market data (zero network calls)."""
    return FeatureEngine(market_data=None, config=config).compute(symbol, df, include_market=False)
