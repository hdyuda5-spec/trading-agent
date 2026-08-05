from abc import ABC, abstractmethod
from typing import Optional


class BaseStrategy(ABC):
    name = "base"

    def __init__(self, config, strat_cfg, exchange, notifier, feature_engine=None):
        self.config = config
        self.strat_cfg = strat_cfg
        self.exchange = exchange
        self.notifier = notifier
        self.feature_engine = feature_engine

    @abstractmethod
    def generate_signal(self, symbol, df, features=None):
        raise NotImplementedError

    def resolve_features(self, symbol, df, features=None):
        """Return the FeatureSet for this signal evaluation.

        Priority: an explicitly-passed FeatureSet, then the injected engine,
        then a df-only (network-free) FeatureSet so legacy callers of
        ``generate_signal(symbol, df)`` keep working unchanged.
        """
        if features is not None:
            return features
        if self.feature_engine is not None:
            return self.feature_engine.compute(symbol, df, include_market=False)
        from agent.features.engine import compute_df_features

        return compute_df_features(symbol, df, self.config)

    def pre_trade(self, symbol, signal):
        return signal
