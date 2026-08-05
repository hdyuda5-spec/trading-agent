from agent.strategies.momentum import MomentumStrategy
from agent.strategies.ai_signal import AISignalStrategy
from agent.strategies.grid import GridStrategy
from agent.strategies.mean_reversion import MeanReversionStrategy

STRATEGY_MAP = {
    "momentum": MomentumStrategy,
    "ai_signal": AISignalStrategy,
    "grid": GridStrategy,
    "mean_reversion": MeanReversionStrategy,
}


def build_strategies(config, exchange, notifier):
    strategies = []
    for name, cfg in config["strategies"].items():
        if cfg.get("enabled") and name in STRATEGY_MAP:
            strategies.append(STRATEGY_MAP[name](config, cfg, exchange, notifier))
    return strategies
