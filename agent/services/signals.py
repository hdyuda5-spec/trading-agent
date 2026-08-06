"""Signal service — turns a fresh candle into an executed trade.

The service is the consumer of the ``FeatureEngine -> DecisionEngine`` stack
for the live strategies (momentum, ai_signal). It reads every indicator from
feature outputs (via the strategies), applies the shared pre-trade filters,
lets the ``RiskEngine`` gate the entry, and hands the Decision to the
ExecutionService. It never places orders itself.
"""

import logging
import threading

from agent.core.utils import is_valid_atr
from agent.services.filters import TradeFilters

logger = logging.getLogger("trading-agent")


class SignalService:
    def __init__(self, config, exchange, notifier, risk, execution, feature_engine, strategies, trend, whale, portfolio, candles):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.risk = risk
        self.execution = execution
        self.feature_engine = feature_engine
        self.strategies = strategies
        self.trend = trend
        self.whale = whale
        self.portfolio = portfolio
        self.candles = candles
        self.filters = TradeFilters(config)
        self.one_per_symbol = config["execution"].get("one_position_per_symbol", True)
        self._lock = threading.RLock()

    def evaluate(self, symbol, df, positions, equity, atr) -> None:
        if df is None or len(df) < 5:
            return
        for strategy in self.strategies:
            if strategy.name not in ("momentum", "ai_signal"):
                continue
            try:
                self._evaluate_strategy(strategy, symbol, df, positions, equity, atr)
            except Exception as e:
                logger.exception("%s evaluate %s failed: %s", strategy.name, symbol, e)

    def _evaluate_strategy(self, strategy, symbol, df, positions, equity, atr) -> None:
        features = self.feature_engine.compute(symbol, df, include_market=False)
        signal = strategy.generate_signal(symbol, df, features=features)
        if not signal:
            return
        if not is_valid_atr(atr):
            logger.info("%s skipped for %s: ATR invalid (%s)", strategy.name, symbol, atr)
            return
        if self.trend.enabled:
            if not self.trend.is_trending(symbol):
                adx = self.trend.last_adx(symbol)
                logger.info(
                    "[SKIP] %s ADX %s < %s, market ranging",
                    symbol,
                    f"{adx:.1f}" if adx is not None else "NaN",
                    self.trend.adx_min,
                )
                return
            trend_dir = self.trend.direction(symbol)
            if trend_dir and trend_dir != signal["side"]:
                logger.info("%s skipped for %s: trend %s vs %s", strategy.name, symbol, trend_dir, signal["side"])
                return
        if self.config.get("whale", {}).get("filter_trades", True):
            wok, wreason = self.filters.whale_filter_ok(symbol, signal["side"], self.whale)
            if not wok:
                logger.info("%s skipped for %s: %s", strategy.name, symbol, wreason)
                return
        if not self.filters.market_regime_allows(signal["side"], self.whale, self._whale_symbols):
            logger.info("%s skipped for %s: market regime memblokir %s", strategy.name, symbol, signal["side"])
            return
        with self._lock:
            open_pos = self.portfolio.open_position(symbol, positions)
            if open_pos and open_pos["side"] != signal["side"]:
                self.notifier.info(f"[REVERSE] {symbol}: close {open_pos['side']} before {signal['side']}")
                self.portfolio.close_position(open_pos["pos"], "reversal")
                positions = self.portfolio.positions()
                open_pos = None
            if self.one_per_symbol and open_pos:
                logger.info("%s skipped for %s: %s position already open", strategy.name, symbol, open_pos["side"])
                return
            if self.filters.losing_streak(symbol, signal["side"], self.portfolio.store):
                logger.info("%s skipped for %s: pola kalah beruntun", strategy.name, symbol)
                return
            fee_pct = 0.02 if self.config["execution"]["order_type"] == "limit" else 0.05
            cost_ok, spread = self.risk.check_fee_tolerance(symbol, fee_pct)
            if not cost_ok:
                logger.info("%s skipped for %s: cost too high (spread=%s%%)", strategy.name, symbol, spread)
                return
            allowed, reason = self.risk.can_open(symbol, positions, equity, signal["price"], signal["side"])
            if not allowed:
                logger.info("%s skipped for %s: %s", strategy.name, symbol, reason)
                return
            entry = signal["price"]
            decision_risk = signal.get("risk") or {}
            sl = decision_risk.get("sl") or self.risk.build_stop_loss(entry, signal["side"], atr)
            tp1 = decision_risk.get("tp") or self.risk.build_take_profit(entry, signal["side"], atr)
            tp2 = tp1 + (tp1 - entry)
            self.notifier.send_signal(symbol, signal["side"], entry, sl, tp1, tp2, strategy.name)
            setup = self.portfolio.capture_setup(
                symbol, signal["side"], entry, atr, signal.get("metadata"), signal.get("confidence")
            )
            self.portfolio.set_trade_meta(symbol, strategy.name, setup, signal.get("confidence"))
            self.execution.open_position(symbol, signal, equity, atr)

    def _whale_symbols(self):
        symbols = list(self.config["symbols"])
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            tickers = {}
        rows = [
            (s, float(t.get("quoteVolume") or 0))
            for s, t in tickers.items()
            if s.endswith("/USDT:USDT")
        ]
        rows.sort(key=lambda r: -r[1])
        for s, _ in rows[: int(self.config.get("whale", {}).get("max_coins", 15))]:
            if s not in symbols:
                symbols.append(s)
        return symbols
