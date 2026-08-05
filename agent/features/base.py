"""Feature Engine core abstractions.

`FeatureResult` is the immutable, structured output every feature produces::

    {
        "trend": "bullish",          # feature-name -> signal
        "confidence": 0.83,          # 0.0 - 1.0
        "metadata": {...}            # supporting raw values for consumers
    }

`BaseFeature` is the abstract interface all features implement. Features are
pure-ish: df-only features never touch the network; market-data features
degrade to `unavailable()` (neutral) when data cannot be obtained, matching
the fail-open behavior the rest of the codebase already uses.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class FeatureResult:
    """Immutable structured output of a single feature."""

    name: str
    signal: str
    confidence: float
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", round(min(1.0, max(0.0, float(self.confidence))), 4))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata or {})))

    @property
    def is_neutral(self) -> bool:
        return self.signal == "neutral"

    def to_dict(self) -> dict:
        """Serialize in the canonical Feature Engine shape."""
        return {self.name: self.signal, "confidence": self.confidence, "metadata": dict(self.metadata)}

    def as_mapping(self) -> dict:
        return {"signal": self.signal, "confidence": self.confidence, "metadata": dict(self.metadata)}

    @classmethod
    def neutral(cls, name: str, confidence: float = 0.5, metadata: Optional[Mapping[str, Any]] = None) -> "FeatureResult":
        return cls(name=name, signal="neutral", confidence=confidence, metadata=dict(metadata or {}))


class BaseFeature(ABC):
    """Abstract base class for all market features."""

    name: str = "base"
    df_only: bool = False
    opt_in: bool = False
    cache_ttl: float = 0.0

    def __init__(self, market_data: Optional[Any], config: Optional[dict]) -> None:
        self.market_data = market_data
        self.config = config or {}
        self._load_config()

    def _load_config(self) -> None:
        cfg = self.config.get("features", {}).get(self.name, {}) or {}
        self.cache_ttl = float(cfg.get("cache_ttl", self.cache_ttl))

    @abstractmethod
    def compute(self, symbol: str, df: Any) -> FeatureResult:
        """Compute the feature for `symbol` from candles `df`.

        `df` is a pandas DataFrame with columns open/high/low/close/volume.
        Must never raise: on any internal error return `unavailable()`.
        """

    def unavailable(self, reason: str = "market data unavailable") -> FeatureResult:
        return FeatureResult.neutral(self.name, metadata={})
