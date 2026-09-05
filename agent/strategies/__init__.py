from agent.strategies.momentum import MomentumStrategy
from agent.strategies.ai_signal import AISignalStrategy
from agent.strategies.grid import GridStrategy
from agent.strategies.mean_reversion import MeanReversionStrategy
from agent.strategies.support_resistance import SupportResistanceStrategy

STRATEGY_MAP = {
    "momentum": MomentumStrategy,
    "ai_signal": AISignalStrategy,
    "grid": GridStrategy,
    "mean_reversion": MeanReversionStrategy,
    "support_resistance": SupportResistanceStrategy,
}


def build_strategies(config, exchange, notifier, feature_engine=None):
    strategies = []
    for name, cfg in config["strategies"].items():
        if cfg.get("enabled") and name in STRATEGY_MAP:
            strategies.append(
                STRATEGY_MAP[name](config, cfg, exchange, notifier, feature_engine=feature_engine)
            )
    return strategies
