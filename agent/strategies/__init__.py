from agent.strategies.grid import GridStrategy
from agent.strategies.mean_reversion import MeanReversionStrategy
from agent.strategies.momentum import MomentumStrategy
from agent.strategies.support_resistance import SupportResistanceStrategy

# ``ai_signal`` is an optional LLM advisory strategy: it is loaded lazily (only
# when explicitly enabled in config) so the core runtime never imports or
# requires the LLM module. Registered here for config compatibility.
STRATEGY_MAP = {
    "momentum": MomentumStrategy,
    "ai_signal": None,  # optional module, loaded only when enabled
    "grid": GridStrategy,
    "mean_reversion": MeanReversionStrategy,
    "support_resistance": SupportResistanceStrategy,
}

_AI_SIGNAL = None


def _ai_signal_class():
    global _AI_SIGNAL
    if _AI_SIGNAL is None:
        from agent.strategies.ai_signal import AISignalStrategy

        _AI_SIGNAL = AISignalStrategy
    return _AI_SIGNAL


def build_strategies(config, exchange, notifier, feature_engine=None):
    strategies = []
    for name, cfg in config["strategies"].items():
        if not cfg.get("enabled"):
            continue
        if name == "ai_signal":
            strategies.append(
                _ai_signal_class()(config, cfg, exchange, notifier, feature_engine=feature_engine)
            )
            continue
        if name in STRATEGY_MAP and STRATEGY_MAP[name] is not None:
            strategies.append(
                STRATEGY_MAP[name](config, cfg, exchange, notifier, feature_engine=feature_engine)
            )
    return strategies