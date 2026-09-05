"""Screener service — periodic auto-screen and auto-trade candidate flow."""

import logging
import threading
import time

from agent.core.screener import Screener
from agent.core.utils import is_valid_atr
from agent.execution.notifier import fmt_wib
from agent.services.filters import TradeFilters
from agent.strategies.scoring import ScreenerScorer, ScreeningGate, TRACE_PREFIX

logger = logging.getLogger("trading-agent")


class ScreenerService:
    def __init__(self, config, exchange, notifier, risk, execution, feature_engine, whale, portfolio, store, candles,
                 *, decision_engine=None, funnel=None, missed_journal=None):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.risk = risk
        self.execution = execution
        self.feature_engine = feature_engine
        self.whale = whale
        self.portfolio = portfolio
        self.store = store
        self.candles = candles
        self.decision_engine = decision_engine
        self.funnel = funnel
        self.missed_journal = missed_journal
        self.filters = TradeFilters(config)
        self.scorer = ScreenerScorer(config, exchange)
        self.gate = ScreeningGate(config, risk, execution, exchange, portfolio)
        self._last_auto_screen = float(store.load_state("last_auto_screen", 0.0) or 0.0)
        self._min_equity = float(config["risk"].get("min_equity_usdt", 20))
        self._lock = threading.RLock()

    def should_run(self, now=None) -> bool:
        sc = self.config.get("screener", {})
        if not sc.get("enabled", True):
            return False
        interval = int(sc.get("auto_interval_minutes", 30)) * 60
        if interval <= 0:
            return False
        return time.time() - self._last_auto_screen >= interval

    def mark_run(self) -> None:
        self._last_auto_screen = time.time()
        self.store.save_state("last_auto_screen", self._last_auto_screen)

    def run(self) -> None:
        self.mark_run()
        try:
            screener = Screener(self.exchange, self.config, feature_engine=self.feature_engine)
            results = screener.candidates()
            title = f"📡 Auto-Screen {screener.timeframe} • {fmt_wib()}"
            self.notifier.send(screener.format(results, title=title))
            top = int(self.config.get("screener", {}).get("top_signal_cards", 3))
            count = 0
            for r in results:
                if count >= top:
                    break
                if r["trend"] == "NEUTRAL" or not r.get("price"):
                    continue
                atr = r.get("atr") or 0.0
                if not is_valid_atr(atr):
                    self.notifier.info(f"Auto-screen skip {r['symbol']}: ATR invalid")
                    continue
                entry = r["price"]
                side_arg = "buy" if r["trend"] == "LONG" else "sell"
                sl = self.risk.build_stop_loss(entry, side_arg, atr)
                tp1 = self.risk.build_take_profit(entry, side_arg, atr)
                if sl is None or tp1 is None:
                    self.notifier.info(f"Auto-screen skip {r['symbol']}: SL/TP tidak valid")
                    continue
                self.notifier.send_signal(r["symbol"], r["trend"], entry, sl, tp1, "screener")
                count += 1
            self.auto_trade_candidates(results)
        except Exception as e:
            self.notifier.alert("Auto-screen gagal", str(e))

    def auto_trade_candidates(self, results) -> None:
        sc = self.config.get("screener", {})
        if not sc.get("auto_trade", False):
            return
        if not self.filters.trading_hours_ok():
            logger.info("[TRACE] auto-trade skip: di luar jam trading (no_trade_hours_wib)")
            return
        top = int(sc.get("top_trade_candidates", sc.get("top_signal_cards", 3)))
        try:
            equity = self.portfolio.equity()
        except Exception as e:
            logger.info("[TRACE] auto-trade batal: equity tidak terbaca (%s)", e)
            return
        if not self.gate.exchange_available():
            logger.info("%s auto-trade batal exchange_unavailable", TRACE_PREFIX)
            return
        ok, category, detail = self.gate.check_global(equity)
        if not ok:
            logger.info("%s auto-trade batal %s: %s", TRACE_PREFIX, category, detail)
            return
        count = 0
        for r in results:
            if count >= top:
                break
            if r["trend"] == "NEUTRAL" or not r.get("price"):
                continue
            symbol = r["symbol"]
            with self._lock:
                try:
                    positions = self.portfolio.positions()
                except Exception:
                    positions = []
                if self.portfolio.open_position(symbol, positions):
                    self.notifier.info(f"Auto-trade skip {symbol}: posisi sudah terbuka")
                    continue
                if self.filters.losing_streak(symbol, r["trend"], self.store):
                    logger.info("[TRACE] %s REJECT streak_loss_blacklist", symbol)
                    continue
                if not is_valid_atr(r.get("atr")):
                    logger.info("[TRACE] %s REJECT invalid_atr", symbol)
                    continue
                df = self._fetch_df(symbol)
                if df is None or len(df) < 5:
                    logger.info("[TRACE] %s REJECT data_tidak_tersedia", symbol)
                    continue
                verdict = self.scorer.evaluate(
                    symbol,
                    df,
                    chg=r.get("chg"),
                    news=sc.get("news"),
                    losing_streak=self.filters.losing_streak(symbol, r["trend"], self.store),
                    market_regime=self._market_regime(),
                )
                if verdict.get("rejected"):
                    self.scorer.log_trace(symbol, verdict, "REJECT")
                    continue
                side = verdict["side"]
                decision = self.scorer.to_decision(verdict)
                ok, category, detail, equity_eff = self.gate.check_order(
                    symbol, side, r["price"], r.get("atr") or 0.0, equity, positions
                )
                if not ok:
                    self.scorer.log_trace(symbol, verdict, "REJECT", detail=f"{category}: {detail}")
                    continue
                if self.decision_engine is not None:
                    dv = self.decision_engine.assess(
                        symbol, df, features=None,
                        strategy_side=side, signal_conf=decision["confidence"],
                        equity=equity, positions=positions,
                        price=r["price"], sl=None, tp=None, atr=r.get("atr") or 0.0,
                    )
                    self._record_decision(dv)
                    if dv.should_block():
                        self._blocked(symbol, side, "screener", dv)
                        continue
                signal = {
                    "strategy": "screener",
                    "symbol": symbol,
                    "side": side,
                    "action": decision["action"],
                    "confidence": decision["confidence"],
                    "price": r["price"],
                    "reason": decision["reason"],
                    "metadata": {
                        "rsi": r.get("rsi"),
                        "vol": r.get("vol"),
                        "chg": r.get("chg"),
                        "confidence_score": verdict["confidence"],
                        "pattern": (r.get("pattern") or {}).get("name"),
                        "smart_money": (r.get("smart_money") or {}).get("direction"),
                    },
                }
                setup = self.portfolio.capture_setup(
                    symbol, side, r["price"], r.get("atr") or 0.0, signal.get("metadata"), decision["confidence"]
                )
                self.portfolio.set_trade_meta(symbol, "screener", setup, decision["confidence"])
                self.scorer.log_trace(symbol, verdict, "ACCEPT")
                self.execution.open_position(symbol, signal, equity_eff, r.get("atr") or 0.0)
                count += 1

    # -- decision gateway (explainable veto on the screener path) ----------

    def _record_decision(self, dv):
        if self.store is None:
            return
        try:
            self.store.save_decision(
                symbol=dv.symbol, status=dv.status, action=dv.action,
                score=dv.score, confidence=dv.confidence, regime=dv.regime,
                reason_code=dv.reason_code, reasons=dv.reasons,
                evidence=dv.evidence_dicts(), weight=dv.weights,
            )
        except Exception:
            pass

    def _blocked(self, symbol, side, strategy, dv):
        code = dv.reason_code or "UNKNOWN"
        if self.funnel:
            self.funnel.reject(code)
            try:
                self.funnel.save()
            except Exception:
                pass
        if self.missed_journal is not None:
            try:
                self.missed_journal.record(
                    symbol, side, strategy, code,
                    dv.rejection.reason if dv.rejection else "",
                    score=dv.score,
                )
            except Exception:
                pass
        logger.info("[DECISION] %s blocked %s: %s (%s)", symbol, side, code, dv.regime)

    def _fetch_df(self, symbol):
        try:
            from agent.core.utils import ohlcv_to_dataframe

            timeframe = self.config.get("screener", {}).get("timeframe", "1h")
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=100)
            return ohlcv_to_dataframe(ohlcv)
        except Exception:
            return None

    def _market_regime(self):
        try:
            if (self.config.get("whale", {}) or {}).get("market_regime", {}).get("enabled", False):
                return self.whale.market_net_flow(self._whale_symbols)
        except Exception:
            return None
        return None

    def _whale_symbols(self):
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            tickers = {}
        symbols = list(self.config["symbols"])
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
