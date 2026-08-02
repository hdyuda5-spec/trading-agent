import concurrent.futures
import logging
import threading
import time

from agent.core.exchange import ExchangeClient
from agent.core.risk import RiskManager
from agent.core.screener import Screener
from agent.core.trend import TrendFilter
from agent.core.utils import compute_atr, ohlcv_to_dataframe
from agent.core.whale import WhaleDetector
from agent.data.trades import TradeStore
from agent.execution.notifier import Notifier, fmt_wib
from agent.execution.order import OrderManager
from agent.execution.telegram_ctl import TelegramController
from agent.strategies import build_strategies

logger = logging.getLogger("trading-agent")


class TradingBot:
    def __init__(self, config):
        self.config = config
        self.notifier = Notifier(config.get("telegram", {}))
        self.exchange = ExchangeClient(
            name=config["exchange"]["name"],
            testnet=config["exchange"].get("testnet", True),
            options=config["exchange"].get("options", {}),
        )
        self.risk = RiskManager(config["risk"], self.exchange)
        self.orders = OrderManager(self.exchange, self.risk, config, self.notifier)
        self.strategies = build_strategies(config, self.exchange, self.notifier)
        self.trend = TrendFilter(self.exchange, config)
        self.store = TradeStore()
        self.telegram = TelegramController(self)
        self.interval = config["execution"]["poll_interval_seconds"]
        self.one_per_symbol = config["execution"].get("one_position_per_symbol", True)
        self.report_hour = config.get("reporting", {}).get("daily_hour", 8)
        self.initial_equity = None
        self.halted = False
        self._min_equity = float(config["risk"].get("min_equity_usdt", 20))
        self._low_balance_halted = False
        self._low_equity_notified = False
        self.trailing = self.store.load_state("trailing", {}) or {}
        self._position_strategy = {}
        self._position_setup = {}
        self._last_auto_screen = self.store.load_state("last_auto_screen", 0.0) or 0.0
        self._last_whale_scan = 0.0
        self.whale = WhaleDetector(self.exchange, config, self.notifier)
        self._positions = {}
        self._dfs = {}
        self._last_metrics_day = ""
        self._start_time = time.time()
        self._last_tick = time.time()
        self._trade_lock = threading.Lock()

    def run(self):
        if not self.exchange.check_health():
            self.notifier.alert("Exchange unreachable, aborting", "")
            return
        self.notifier.info(f"Agent started on {self.config['exchange']['name']} (testnet={self.exchange.testnet})")
        equity = self._equity()
        baseline = self.store.load_state("equity_baseline", None)
        today = time.strftime("%Y-%m-%d")
        if baseline and baseline.get("date") == today and baseline.get("equity", 0) > 0:
            self.initial_equity = baseline["equity"]
        else:
            self.initial_equity = equity
            self.store.save_state("equity_baseline", {"date": today, "equity": equity})
        self.risk.set_initial_equity(self.initial_equity)
        self.telegram.start()
        while True:
            try:
                self.tick()
            except Exception as e:
                self.notifier.alert("Tick error", str(e))
            time.sleep(self.interval)

    def tick(self):
        self._last_tick = time.time()
        equity = self._equity()
        if self.risk.below_min_equity(equity):
            if not self._low_balance_halted:
                self._low_balance_halted = True
                self.notifier.alert(
                    f"Equity {equity:.2f} USDT below minimum {self._min_equity} USDT. "
                    f"Trading paused until balance is topped up.",
                    "",
                )
            return
        if self._low_balance_halted and equity >= self._min_equity:
            self._low_balance_halted = False
            self.notifier.info(f"Equity recovered to {equity:.2f} USDT, resuming trading")
        baseline = self.store.load_state("equity_baseline", None)
        today = time.strftime("%Y-%m-%d")
        if not baseline or baseline.get("date") != today:
            self.store.save_state("equity_baseline", {"date": today, "equity": equity})
            self.initial_equity = equity
            self.risk.set_initial_equity(equity)
        elif baseline.get("equity", 0) > 0 and equity > baseline["equity"] * 1.20:
            self.store.save_state("equity_baseline", {"date": today, "equity": equity})
            self.initial_equity = equity
            self.risk.set_initial_equity(equity)
            self.notifier.info(
                f"Equity top-up terdeteksi: baseline direset {baseline['equity']:.2f} -> {equity:.2f} USDT"
            )
        if not self.initial_equity and equity > 0:
            self.initial_equity = equity
            self.risk.set_initial_equity(equity)
            self.notifier.info(f"Initial equity set: {equity:.2f} USDT")
        self.risk.update_equity(equity)
        positions = self.exchange.fetch_positions()
        self._manage_positions(equity, positions)
        if self.halted:
            return
        self._guard_sl_tp(positions)
        self.orders.cancel_stale_orders(
            ttl=self.config["execution"].get("order_ttl_seconds", 0),
            symbol=None if self._grid_enabled() else None,
        )
        min_eq = self.config["risk"].get("min_equity", 0)
        low_equity = min_eq > 0 and equity < min_eq
        if low_equity and not self._low_equity_notified:
            self.notifier.alert(
                f"[LOW BALANCE] equity={equity:.2f} < min_equity={min_eq:.2f}; entri baru dijeda", ""
            )
            self._low_equity_notified = True
        elif not low_equity:
            self._low_equity_notified = False
        if low_equity:
            return
        self._fetch_ohlcv_parallel()
        atr_period = self.config["risk"].get("atr_period", 14)
        for symbol in self.config["symbols"]:
            df = self._dfs.get(symbol)
            if df is None or len(df) < atr_period + 2:
                continue
            atr = float(compute_atr(df, atr_period).iloc[-1])
            self._run_momentum_and_ai(symbol, df, positions, equity, atr)
            self._run_grid(symbol, positions, equity, atr)
        self.store.save_state("trailing", self.trailing)
        self._maybe_daily_report()
        self._maybe_auto_screen()
        self._maybe_whale_scan()

    def _grid_enabled(self):
        return self.config["strategies"].get("grid", {}).get("enabled", False)

    def _maybe_daily_report(self):
        today = time.strftime("%Y-%m-%d")
        if time.localtime().tm_hour != self.report_hour or today == self._last_metrics_day:
            return
        self._last_metrics_day = today
        self.notifier.info(f"[REPORT] daily report {today}")
        self.telegram.send_daily_report()

    def _maybe_auto_screen(self):
        sc = self.config.get("screener", {})
        if not sc.get("enabled", True):
            return
        interval = int(sc.get("auto_interval_minutes", 30)) * 60
        if interval <= 0 or time.time() - self._last_auto_screen < interval:
            return
        self._last_auto_screen = time.time()
        self.store.save_state("last_auto_screen", self._last_auto_screen)
        threading.Thread(target=self._run_auto_screen, daemon=True).start()

    def _maybe_whale_scan(self):
        wcfg = self.config.get("whale", {})
        if not wcfg.get("enabled", True):
            return
        interval = int(wcfg.get("interval_minutes", 10)) * 60
        if interval <= 0 or time.time() - self._last_whale_scan < interval:
            return
        self._last_whale_scan = time.time()
        threading.Thread(target=self._run_whale_scan, daemon=True).start()

    def _run_whale_scan(self):
        try:
            events = self.whale.scan(self._whale_symbols())
            if not events:
                return
            min_net = float(self.config.get("whale", {}).get("min_alert_net_usdt", 0))
            if min_net > 0 and max(abs(e["net_usdt"]) for e in events) < min_net:
                return
            self.notifier.send(self.whale.format(events))
        except Exception as e:
            self.notifier.alert("Whale scan gagal", str(e))

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

    def _run_auto_screen(self):
        try:
            screener = Screener(self.exchange, self.config)
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
                entry = r["price"]
                atr = r.get("atr") or 0.0
                sl = self.risk.build_stop_loss(entry, r["trend"], atr)
                tp1 = self.risk.build_take_profit(entry, r["trend"], atr)
                tp2 = tp1 + (tp1 - entry)
                self.notifier.send_signal(r["symbol"], r["trend"], entry, sl, tp1, tp2, "screener")
                count += 1
            self._auto_trade_candidates(results)
        except Exception as e:
            self.notifier.alert("Auto-screen gagal", str(e))

    def _auto_trade_candidates(self, results):
        sc = self.config.get("screener", {})
        if not sc.get("auto_trade", False):
            return
        top = int(sc.get("top_trade_candidates", sc.get("top_signal_cards", 3)))
        try:
            equity = self._equity()
        except Exception:
            return
        min_eq = self.config["risk"].get("min_equity", 0)
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
            with self._trade_lock:
                try:
                    positions = self.exchange.fetch_positions()
                except Exception:
                    positions = []
                if any(abs(float(p.get("contracts") or 0)) > 0 and p.get("symbol") == symbol for p in positions):
                    self.notifier.info(f"Auto-trade skip {symbol}: posisi sudah terbuka")
                    continue
                if (side == "LONG" and r["rsi"] >= 70) or (side == "SHORT" and r["rsi"] <= 30):
                    self.notifier.info(f"Auto-trade skip {symbol}: rsi={r['rsi']} (overbought/oversold)")
                    continue
                max_ext = float(self.config.get("screener", {}).get("max_extended_pct", 0))
                if max_ext > 0 and abs(float(r["chg"])) > max_ext:
                    self.notifier.info(f"Auto-trade skip {symbol}: move {r['chg']}% terlalu ekstrem")
                    continue
                if self._losing_streak(symbol, side):
                    self.notifier.info(f"Auto-trade skip {symbol}: pola kalah beruntun")
                    continue
                ok, equity_eff, reason = self._screen_trade_ok(symbol, side, r["price"], r.get("atr") or 0.0, equity, positions)
                if not ok:
                    self.notifier.info(f"Auto-trade skip {symbol}: {reason}")
                    continue
                if self.config.get("screener", {}).get("require_confirmation", True):
                    conflict = self._conflict_reason(r, side)
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
                self._position_strategy[symbol] = "screener"
                self._position_setup[symbol] = self._capture_setup(symbol, side, r["price"], r.get("atr") or 0.0, signal.get("metadata"))
                self.orders.open_position(symbol, signal, equity_eff, r.get("atr") or 0.0)
                count += 1

    def _conflict_reason(self, r, side):
        pat = r.get("pattern") or {}
        if pat.get("direction") == "bullish" and side == "SHORT":
            return f"pattern {pat['name']} kontra"
        if pat.get("direction") == "bearish" and side == "LONG":
            return f"pattern {pat['name']} kontra"
        sm = r.get("smart_money") or {}
        if sm.get("direction") == "LONG" and side == "SHORT":
            return "smart money LONG vs SHORT"
        if sm.get("direction") == "SHORT" and side == "LONG":
            return "smart money SHORT vs LONG"
        return None

    def _losing_streak(self, symbol, side):
        limit = int(self.config["risk"].get("skip_after_consecutive_losses", 0))
        if limit <= 0:
            return False
        recent = self.store.recent_trades(symbol, side, limit)
        return len(recent) >= limit and all(r["pnl"] < 0 for r in recent)

    def _screen_trade_ok(self, symbol, side, price, atr, equity, positions):
        m = self.exchange.client.markets.get(symbol)
        if m is None:
            return False, None, "market tidak ditemukan"
        limits = m.get("limits", {})
        min_cost = float((limits.get("cost") or {}).get("min") or 0)
        min_amt = float((limits.get("amount") or {}).get("min") or 0)
        max_pos_pct = self.config["risk"].get("max_position_pct", 60) / 100.0
        exposure_pct = self.config["risk"].get("max_total_exposure_pct", 90) / 100.0
        ok, spread = self.risk.check_fee_tolerance(symbol)
        if not ok:
            return False, None, f"spread/fee {spread}%"
        try:
            pending = self.risk._pending_notional()
        except Exception:
            pending = 0.0
        used = 0.0
        for p in positions:
            if abs(float(p.get("contracts") or 0)) > 0:
                used += abs(float(p.get("notional") or 0))
        budget = max(0.0, equity * exposure_pct - used - pending)
        if min_cost and budget < min_cost:
            return False, None, f"budget {budget:.2f} < minCost {min_cost:.0f}"
        factor = 1.0
        if atr and price:
            atr_pct = atr / price
            if atr_pct > 0:
                normal_pct = self.config["risk"].get("atr_normal_pct", 1.0) / 100.0
                bounds = self.config["risk"].get("size_volatility_bounds", [0.9, 2.0])
                factor = max(bounds[0], min(bounds[1], normal_pct / atr_pct))
        desired = min(equity * max_pos_pct * factor, budget)
        if desired <= 0:
            return False, None, "budget habis"
        equity_eff = desired / (max_pos_pct * factor) if max_pos_pct * factor > 0 else equity
        qty = self.risk.compute_position_size(symbol, price, equity_eff, "buy" if side == "LONG" else "sell", atr)
        try:
            qty = float(self.exchange.client.amount_to_precision(symbol, qty))
        except Exception:
            return False, None, "qty di bawah presisi pasar"
        cost = qty * price
        if min_cost and cost < min_cost:
            return False, None, f"minCost {min_cost:.0f} > {cost:.2f}"
        if min_amt and qty < min_amt:
            return False, None, f"minAmt {min_amt} > {qty}"
        allowed, reason = self.risk.can_open(symbol, positions, equity, price, side)
        if not allowed:
            return False, None, reason
        return True, equity_eff, "ok"

    def _fetch_ohlcv_parallel(self):
        symbols = self.config["symbols"]
        timeframe = self.config["timeframe"]
        self._dfs = {}
        workers = min(len(symbols), 4)
        if workers <= 0:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.exchange.fetch_ohlcv, s, timeframe, 200): s for s in symbols}
            for f in concurrent.futures.as_completed(futures):
                symbol = futures[f]
                try:
                    self._dfs[symbol] = ohlcv_to_dataframe(f.result())
                except Exception as e:
                    logger.warning("ohlcv %s failed: %s", symbol, e)

    def _manage_positions(self, equity, positions):
        if self.risk.daily_loss_exceeded(equity):
            self.notifier.alert("Daily loss limit reached, closing all positions", "")
            seen = set()
            for pos in positions:
                symbol = pos.get("symbol")
                if not symbol or symbol in seen or abs(float(pos.get("contracts") or 0)) == 0:
                    continue
                seen.add(symbol)
                self._close_position(pos, "daily_loss")
            self.halted = True
            return
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            tickers = {}
        for pos in positions:
            contracts = float(pos.get("contracts") or 0)
            if contracts == 0 or not pos.get("symbol"):
                continue
            symbol = pos["symbol"]
            side = self._pos_side(pos)
            last = float(tickers.get(symbol, {}).get("last") or 0)
            if last <= 0:
                continue
            entry = float(pos.get("entryPrice") or 0)
            peak = self.trailing.get(symbol, {}).get("peak")
            if peak is None or (side == "long" and last > peak) or (side == "short" and last < peak):
                peak = last
            self.trailing[symbol] = {"side": side, "peak": peak, "entry": entry}
            atr = None
            df = self._dfs.get(symbol)
            if df is not None and len(df) > 2:
                try:
                    atr = float(compute_atr(df, self.config["risk"].get("atr_period", 14)).iloc[-1])
                except Exception:
                    atr = None
            if self.risk.trailing_stop_hit(side, peak, last, atr):
                self.notifier.info(f"[TRAILING] closing {symbol} {side} at {last}")
                self._close_position(pos, "trailing", last)
        self._detect_external_closes(positions)
        self._positions = {
            p.get("symbol"): p for p in positions if abs(float(p.get("contracts") or 0)) > 0
        }

    def _detect_external_closes(self, positions):
        current = {p.get("symbol") for p in positions if abs(float(p.get("contracts") or 0)) > 0}
        for symbol, old in list(self._positions.items()):
            if symbol in current:
                continue
            self._positions.pop(symbol, None)
            self._close_position(old, "sl_tp")

    def _guard_sl_tp(self, positions):
        if not self.config["execution"].get("reduce_only_on_close", True):
            return
        try:
            positions = self.exchange.fetch_positions()
        except Exception:
            return
        for pos in positions:
            contracts = abs(float(pos.get("contracts") or 0))
            if contracts == 0 or not pos.get("symbol"):
                continue
            symbol = pos["symbol"]
            side = "buy" if self._pos_side(pos) == "long" else "sell"
            entry = float(pos.get("entryPrice") or 0)
            if entry <= 0:
                continue
            atr = None
            df = self._dfs.get(symbol)
            if df is not None and len(df) >= 2:
                try:
                    atr = float(compute_atr(df, self.config["risk"].get("atr_period", 14)).iloc[-1])
                except Exception:
                    atr = None
            self.orders.ensure_sl_tp(symbol, side, entry, contracts, atr)

    def _close_position(self, pos, reason, price=0.0):
        symbol = pos.get("symbol")
        if not symbol:
            return
        try:
            live = self.exchange.fetch_positions([symbol])
        except Exception:
            live = []
        live_pos = next((p for p in live if abs(float(p.get("contracts") or 0)) > 0), None)
        if live_pos is None:
            self._position_strategy.pop(symbol, None)
            self._position_setup.pop(symbol, None)
            self._positions.pop(symbol, None)
            self.trailing.pop(symbol, None)
            return
        contracts = abs(float(live_pos.get("contracts") or 0))
        if contracts == 0:
            return
        if price <= 0:
            price = self._price(symbol)
        side = self._pos_side(live_pos)
        entry = float(live_pos.get("entryPrice") or 0)
        pnl = (price - entry) * contracts if side == "long" else (entry - price) * contracts
        pnl_pct = (pnl / (entry * contracts)) * 100 if entry and contracts else 0.0
        strategy = self._position_strategy.pop(symbol, "bot")
        setup = self._position_setup.pop(symbol, {})
        outcome = "win" if pnl >= 0 else "loss"
        lesson = self._build_lesson(symbol, side, strategy, setup, pnl, pnl_pct, reason)
        self.store.record_trade(symbol, side, entry, price, contracts, pnl, pnl_pct, reason, strategy)
        self.store.add_experience(symbol, side, strategy, setup, outcome, reason, pnl, pnl_pct, lesson)
        self.orders.close_all(symbol)
        self._positions.pop(symbol, None)
        self.trailing.pop(symbol, None)
        self.notifier.info(f"[CLOSE:{reason}] {symbol} {side} pnl={pnl:.2f} ({pnl_pct:.1f}%)")
        self.notifier.send_close(symbol, side, entry, price, contracts, pnl, pnl_pct, reason, strategy)

    def _run_momentum_and_ai(self, symbol, df, positions, equity, atr):
        for strategy in self.strategies:
            if strategy.name not in ("momentum", "ai_signal"):
                continue
            signal = strategy.generate_signal(symbol, df)
            if not signal:
                continue
            if self.trend.enabled:
                trend_dir = self.trend.direction(symbol)
                if trend_dir and trend_dir != signal["side"]:
                    logger.info("%s skipped for %s: trend %s vs %s", strategy.name, symbol, trend_dir, signal["side"])
                    continue
            with self._trade_lock:
                try:
                    positions = self.exchange.fetch_positions()
                except Exception:
                    positions = []
                open_pos = self._open_position(symbol, positions)
                if open_pos and open_pos["side"] != signal["side"]:
                    self.notifier.info(f"[REVERSE] {symbol}: close {open_pos['side']} before {signal['side']}")
                    self._close_position(open_pos["pos"], "reversal")
                    positions = self.exchange.fetch_positions()
                    open_pos = None
                if self.one_per_symbol and open_pos:
                    logger.info("%s skipped for %s: %s position already open", strategy.name, symbol, open_pos["side"])
                    continue
                if self._losing_streak(symbol, signal["side"]):
                    logger.info("%s skipped for %s: pola kalah beruntun", strategy.name, symbol)
                    continue
                fee_pct = 0.02 if self.config["execution"]["order_type"] == "limit" else 0.05
                cost_ok, spread = self.risk.check_fee_tolerance(symbol, fee_pct)
                if not cost_ok:
                    logger.info("%s skipped for %s: cost too high (spread=%s%%)", strategy.name, symbol, spread)
                    continue
                allowed, reason = self.risk.can_open(symbol, positions, equity, signal["price"], signal["side"])
                if not allowed:
                    logger.info("%s skipped for %s: %s", strategy.name, symbol, reason)
                    continue
                entry = signal["price"]
                sl = self.risk.build_stop_loss(entry, signal["side"], atr)
                tp1 = self.risk.build_take_profit(entry, signal["side"], atr)
                tp2 = tp1 + (tp1 - entry)
                self.notifier.send_signal(symbol, signal["side"], entry, sl, tp1, tp2, strategy.name)
                self._position_strategy[symbol] = strategy.name
                self._position_setup[symbol] = self._capture_setup(symbol, signal["side"], entry, atr, signal.get("metadata"))
                self.orders.open_position(symbol, signal, equity, atr)

    def _run_grid(self, symbol, positions, equity, atr):
        for strategy in self.strategies:
            if strategy.name != "grid":
                continue
            ticker = self.exchange.fetch_ticker(symbol)
            price = float(ticker["last"])
            open_orders = self.exchange.fetch_open_orders(symbol)
            if not strategy.grids.get(symbol):
                strategy.build_grid(symbol, price)
            elif strategy.needs_rebuild(symbol, price):
                self.orders.cancel_pending(symbol)
                strategy.reset(symbol)
                strategy.build_grid(symbol, price)
                open_orders = self.exchange.fetch_open_orders(symbol)
            amount = strategy.order_size(symbol, equity, price)
            max_orders = strategy.strat_cfg.get("max_grid_orders", 0)
            pending = len(open_orders)
            for level in strategy.reconcile(symbol, positions, open_orders, price):
                if max_orders and pending >= max_orders:
                    break
                try:
                    self.exchange.create_order(
                        symbol,
                        "limit",
                        level["side"],
                        amount=amount,
                        price=level["price"],
                        params={"postOnly": True},
                    )
                    self.notifier.info(f"[GRID] {symbol} {level['side']} @ {level['price']}")
                    self._position_strategy[symbol] = "grid"
                    level["status"] = "placed"
                    pending += 1
                except Exception as e:
                    self.notifier.alert(f"Grid order failed {symbol}", str(e))

    def _capture_setup(self, symbol, side, entry, atr, metadata=None):
        return {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "atr": atr,
            "metadata": metadata or {},
        }

    def _build_lesson(self, symbol, side, strategy, setup, pnl, pnl_pct, reason):
        meta = setup.get("metadata") or {}
        rsi = meta.get("rsi")
        rsi_txt = f", rsi={rsi:.1f}" if rsi is not None else ""
        entry = setup.get("entry")
        entry_txt = f", entry={entry}" if entry else ""
        base = f"{symbol} {side.upper()} via {strategy}: {pnl_pct:+.1f}% ({reason}){entry_txt}{rsi_txt}"
        if pnl >= 0:
            return f"Setup BERHASIL: {base}. Pertahankan pola seperti ini."
        return f"Setup GAGAL: {base}. Jangan ulangi pola yang gagal; tunggu konfirmasi lebih kuat."

    def _open_position(self, symbol, positions):
        for pos in positions:
            if pos.get("symbol") != symbol:
                continue
            contracts = float(pos.get("contracts") or 0)
            if contracts == 0:
                continue
            side = self._pos_side(pos)
            return {"side": "LONG" if side == "long" else "SHORT", "pos": pos}
        return None

    def _pos_side(self, pos):
        side = str(pos.get("side") or "").lower()
        if side in ("long", "short"):
            return side
        amt = (pos.get("info") or {}).get("positionAmt")
        if amt is not None:
            return "long" if float(amt) > 0 else "short"
        return "long" if float(pos.get("contracts") or 0) > 0 else "short"

    def _price(self, symbol):
        try:
            return float(self.exchange.fetch_ticker(symbol)["last"])
        except Exception:
            return 0.0

    def _equity(self):
        balance = self.exchange.fetch_balance()
        return float(balance.get("USDT", {}).get("total", 0) or 0)
