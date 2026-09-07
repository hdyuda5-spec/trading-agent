"""Signal service — turns a fresh candle into an executed trade.

The service is the consumer of the ``FeatureEngine -> DecisionEngine`` stack
for the live strategies (momentum, ai_signal). It reads every indicator from
feature outputs (via the strategies), applies the shared pre-trade filters,
lets the ``RiskEngine`` gate the entry, runs the explainable
``UnifiedDecisionEngine`` (hard veto + evidence score), mints a
``TradeTicket``, and hands it to the ExecutionService. It never places orders
itself.
"""

import logging
import threading

from agent.core.utils import is_valid_atr
from agent.decision.ticket import TradeTicket
from agent.services.filters import TradeFilters

logger = logging.getLogger("trading-agent")


class SignalService:
    def __init__(self, config, exchange, notifier, risk, execution, feature_engine, strategies,
                 trend, whale, portfolio, candles, *, store=None, decision_engine=None,
                 funnel=None, missed_journal=None):
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
        self.store = store
        self.decision_engine = decision_engine
        self.funnel = funnel
        self.missed_journal = missed_journal
        self.filters = TradeFilters(config)
        self.one_per_symbol = config["execution"].get("one_position_per_symbol", True)
        self._lock = threading.RLock()

    def evaluate(self, symbol, df, positions, equity, atr) -> None:
        if df is None or len(df) < 5:
            return
        for strategy in self.strategies:
            if strategy.name not in ("momentum", "ai_signal", "support_resistance"):
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
        if self.funnel:
            self.funnel.inc("strategy_signals")
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
            if not self.filters.trading_hours_ok():
                logger.info("%s skipped for %s: di luar jam trading", strategy.name, symbol)
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
            ticket = self._gate(symbol, df, features, signal, positions, equity, entry, sl, tp1, atr)
            if ticket is None:
                return
            self.notifier.send_signal(symbol, signal["side"], entry, sl, tp1, strategy.name)
            setup = self.portfolio.capture_setup(
                symbol, signal["side"], entry, atr, signal.get("metadata"), signal.get("confidence")
            )
            self.portfolio.set_trade_meta(symbol, strategy.name, setup, signal.get("confidence"))
            self.execution.open_position(symbol, signal, equity, atr, ticket=ticket)

    # -- decision gateway -------------------------------------------------

    def _gate(self, symbol, df, features, signal, positions, equity, entry, sl, tp1, atr):
        """Run the explainable decision engine; return a TradeTicket or None.

        Default thresholds are lenient (0.0), so the engine is informational
        until configured — but hard vetoes (invalid price, SL/TP, daily loss,
        min equity, halted) always win over a strategy signal. A passing
        candidate always walks out with a valid TradeTicket.
        """
        verdict = None
        if self.decision_engine is not None:
            verdict = self.decision_engine.assess(
                symbol, df, features=features,
                advisory=signal.get("advisory"),
                strategy_side=signal["side"],
                signal_conf=signal.get("confidence"),
                equity=equity, positions=positions,
                price=entry, sl=sl, tp=tp1, atr=atr,
            )
            self._record_decision(verdict)
            if verdict.should_block():
                self._blocked(symbol, signal, verdict)
                return None
        ticket = self._build_ticket(symbol, signal, verdict, entry, sl, tp1, atr, equity)
        if ticket is not None:
            self._persist_ticket(ticket)
            if self.funnel:
                self.funnel.inc("tickets_created")
                try:
                    self.funnel.save()
                except Exception:
                    pass
        return ticket

    def _build_ticket(self, symbol, signal, verdict, entry, sl, tp1, atr, equity):
        side = signal["side"]
        if verdict is not None:
            if verdict.side is None:
                return None
            side = verdict.side
        risk_amount = size = rr = None
        try:
            risk_amount = self.risk.risk_per_trade_amount(equity)
            size = self.risk.risk_position_size(symbol, entry, side, atr=atr, equity=equity, stop_loss=sl)
            if sl and tp1:
                _ok, _code, _val = self.risk.validate_rr(entry, sl, tp1, side)
                rr = float(_val)
        except Exception:
            pass
        ttl = float((self.config.get("decision", {}) or {}).get("ticket_ttl_seconds", 300) or 300)
        leverage = getattr(self.risk, "cfg", {}).get("leverage", 1) if hasattr(self.risk, "cfg") else 1.0
        score = float(verdict.score) if verdict is not None else 0.0
        confidence = float(verdict.confidence) if verdict is not None else float(signal.get("confidence") or 0.0)
        regime = verdict.regime if verdict is not None else None
        reasons = list(verdict.reasons) if verdict is not None else [str(r) for r in (signal.get("reason") or [])]
        evidence = verdict.evidence_dicts() if verdict is not None else []
        decision_status = verdict.status if verdict is not None else "PASS"
        return TradeTicket.new(
            symbol, side, signal.get("strategy") or "live", entry,
            stop_loss=sl, take_profit=tp1,
            risk_pct=float((self.config.get("risk", {}) or {}).get("risk_per_trade_pct", 1.0)),
            risk_amount=float(risk_amount or 0.0),
            position_size=size,
            leverage=float(leverage),
            atr=atr, rr=rr,
            score=score, confidence=confidence, regime=regime,
            reasons=reasons, evidence=evidence,
            ttl_seconds=ttl, metadata={"source": "signal_service"},
            equity=equity, source="signal_service", decision_status=decision_status,
        )

    def _record_decision(self, verdict):
        if self.store is None:
            return
        try:
            self.store.save_decision(
                symbol=verdict.symbol, status=verdict.status, action=verdict.action,
                score=verdict.score, confidence=verdict.confidence, regime=verdict.regime,
                reason_code=verdict.reason_code, reasons=verdict.reasons,
                evidence=verdict.evidence_dicts(), weight=verdict.weights,
            )
        except Exception:
            pass

    def _persist_ticket(self, ticket):
        if self.store is None:
            return
        try:
            self.store.save_ticket(ticket)
        except Exception:
            pass

    def _blocked(self, symbol, signal, verdict):
        code = verdict.reason_code or "UNKNOWN"
        if self.funnel:
            self.funnel.reject(code)
            try:
                self.funnel.save()
            except Exception:
                pass
        if self.missed_journal is not None:
            try:
                self.missed_journal.record(
                    symbol, signal["side"], signal.get("strategy") or "live", code,
                    verdict.rejection.reason if verdict.rejection else "",
                    price=signal.get("price"), score=verdict.score,
                )
            except Exception:
                pass
        logger.info("[DECISION] %s blocked %s: %s (%s)", symbol, signal["side"], code, verdict.regime)

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
