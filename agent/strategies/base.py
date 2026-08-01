from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    name = "base"

    def __init__(self, config, strat_cfg, exchange, notifier):
        self.config = config
        self.strat_cfg = strat_cfg
        self.exchange = exchange
        self.notifier = notifier

    @abstractmethod
    def generate_signal(self, symbol, df):
        raise NotImplementedError

    def pre_trade(self, symbol, signal):
        return signal
