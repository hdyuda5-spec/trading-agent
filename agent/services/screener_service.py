"""Screener service — periodic auto-screen and auto-trade candidate flow."""

import logging
import threading
import time

from agent.core.screener import Screener
from agent.core.utils import is_valid_atr
from agent.execution.notifier import fmt_wib
from agent.services.filters import TradeFilters

logger = logging.getLogger("trading-agent")


class ScreenerService:
    def __init__(self, config, exchange, notifier, risk, execution, feature_engine, whale, portfolio, store, candles):
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
        self.filters = TradeFilters(config)
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
                sl = self.risk.build_stop_loss(entry, r["trend"], atr)
                tp1 = self.risk.build_take_profit(entry, r["trend"], atr)
                if sl is None or tp1 is None:
                    self.notifier.info(f"Auto-screen skip {r['symbol']}: SL/TP tidak valid")
                    continue
                tp2 = tp1 + (tp1 - entry)
                self.notifier.send_signal(r["symbol"], r["trend"], entry, sl, tp1, tp2, "screener")
                count += 1
            self.auto_trade_candidates(results)
        except Exception as e:
            self.notifier.alert("Auto-screen gagal", str(e))

    def auto_trade_candidates(self, results) -> None:
        sc = self.config.get("screener", {})
        if not sc.get("auto_trade", False):
            return
        top = int(sc.get("top_trade_candidates", sc.get("top_signal_cards", 3)))
        try:
            equity = self.portfolio.equity()
        except Exception:
            return
        min_eq = self._min_equity
        if min_eq > 0 and equity < min_eq:
            self.notifier.info(f"Auto-trade skip: equity {equity:.2f} < min_equity {min_eq}")
            return
        count = 0
        for r in results:
            if count >= top:
                break
            if r["trend"] == "NEUTRAL" or not r.get("price"):
                continue
            symbol = r["symbol"]
            side = r["trend"]
            with self._lock:
                try:
                    positions = self.portfolio.positions()
                except Exception:
                    positions = []
                if self.portfolio.open_position(symbol, positions):
                    self.notifier.info(f"Auto-trade skip {symbol}: posisi sudah terbuka")
                    continue
                if not self.filters.rsi_confirmation_ok(side, r.get("rsi")):
                    long_r = sc.get("rsi_long_range", [40, 75])
                    short_r = sc.get("rsi_short_range", [25, 60])
                    rng = long_r if side == "LONG" else short_r
                    self.notifier.info(f"Auto-trade skip {symbol}: rsi={r['rsi']:.1f} di luar range {side} {rng}")
                    continue
                if not self.filters.not_extreme_move(r.get("chg")):
                    self.notifier.info(f"Auto-trade skip {symbol}: move {r['chg']}% terlalu ekstrem")
                    continue
                if not is_valid_atr(r.get("atr")):
                    self.notifier.info(f"Auto-trade skip {symbol}: ATR invalid")
                    continue
                if self.filters.losing_streak(symbol, side, self.store):
                    self.notifier.info(f"Auto-trade skip {symbol}: pola kalah beruntun")
                    continue
                wok, wreason = self.filters.whale_filter_ok(symbol, side, self.whale)
                if not wok:
                    self.notifier.info(f"Auto-trade skip {symbol}: {wreason}")
                    continue
                if not self.filters.market_regime_allows(side, self.whale, self._whale_symbols):
                    self.notifier.info(f"Auto-trade skip {symbol}: market regime memblokir {side}")
                    continue
                ok, equity_eff, reason = self.execution.screen_trade_ok(
                    symbol, side, r["price"], r.get("atr") or 0.0, equity, positions
                )
                if not ok:
                    self.notifier.info(f"Auto-trade skip {symbol}: {reason}")
                    continue
                if sc.get("require_confirmation", True):
                    conflict = self.filters.conflict_reason(r, side)
                    if conflict:
                        self.notifier.info(f"Auto-trade skip {symbol}: {conflict}")
                        continue
                signal = {
                    "strategy": "screener",
                    "symbol": symbol,
                    "side": side,
                    "confidence": 70.0,
                    "price": r["price"],
                    "metadata": {
                        "rsi": r["rsi"],
                        "vol": r["vol"],
                        "chg": r["chg"],
                        "pattern": (r.get("pattern") or {}).get("name"),
                        "smart_money": (r.get("smart_money") or {}).get("direction"),
                    },
                }
                setup = self.portfolio.capture_setup(symbol, side, r["price"], r.get("atr") or 0.0, signal.get("metadata"), 70.0)
                self.portfolio.set_trade_meta(symbol, "screener", setup, 70.0)
                self.execution.open_position(symbol, signal, equity_eff, r.get("atr") or 0.0)
                count += 1

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
