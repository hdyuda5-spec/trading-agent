import concurrent.futures
import logging
import time

from agent.core.exchange import ExchangeClient
from agent.core.risk import RiskManager
from agent.core.trend import TrendFilter
from agent.core.utils import compute_atr, ohlcv_to_dataframe
from agent.data.trades import TradeStore
from agent.execution.notifier import Notifier
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
        self.initial_equity = None
        self.halted = False
        self.trailing = self.store.load_state("trailing", {}) or {}
        self._positions = {}
        self._dfs = {}
        self._last_metrics_day = ""

    def run(self):
        if not self.exchange.check_health():
            self.notifier.alert("Exchange unreachable, aborting", "")
            return
        self.notifier.info(f"Agent started on {self.config['exchange']['name']} (testnet={self.exchange.testnet})")
        self.initial_equity = self._equity()
        self.risk.set_initial_equity(self.initial_equity)
        self.telegram.start()
        while True:
            try:
                self.tick()
            except Exception as e:
                self.notifier.alert("Tick error", str(e))
            time.sleep(self.interval)

    def tick(self):
        equity = self._equity()
        self.risk.update_equity(equity)
        positions = self.exchange.fetch_positions()
        self._manage_positions(equity, positions)
        if self.halted:
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
        self._maybe_report_metrics()

    def _maybe_report_metrics(self):
        day = time.strftime("%Y-%m-%d")
        if day == self._last_metrics_day:
            return
        self._last_metrics_day = day
        metrics = self.store.metrics()
        if not metrics:
            return
        self.notifier.info(
            f"[METRICS] trades={metrics['trades']} win_rate={metrics['win_rate']}% "
            f"profit_factor={metrics['profit_factor']} net_pnl={metrics['net_pnl']}"
        )

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
            side = "long" if contracts > 0 else "short"
            last = float(tickers.get(symbol, {}).get("last") or 0)
            if last <= 0:
                continue
            entry = float(pos.get("entryPrice") or 0)
            peak = self.trailing.get(symbol, {}).get("peak")
            if peak is None or (side == "long" and last > peak) or (side == "short" and last < peak):
                peak = last
            self.trailing[symbol] = {"side": side, "peak": peak, "entry": entry}
            if self.risk.trailing_stop_hit(side, peak, last):
                self.notifier.info(f"[TRAILING] closing {symbol} {side} at {last}")
                self._close_position(pos, "trailing", last)
        self._detect_external_closes(positions)
        self._positions = {
            p.get("symbol"): p for p in positions if abs(float(p.get("contracts") or 0)) > 0
        }

    def _detect_external_closes(self, positions):
        current = {p.get("symbol") for p in positions if abs(float(p.get("contracts") or 0)) > 0}
        for symbol, old in self._positions.items():
            if symbol in current or symbol not in self.config["symbols"]:
                continue
            self._close_position(old, "sl_tp")

    def _close_position(self, pos, reason, price=0.0):
        symbol = pos.get("symbol")
        if not symbol:
            return
        contracts = abs(float(pos.get("contracts") or 0))
        signed = float(pos.get("contracts") or 0)
        if contracts == 0:
            return
        if price <= 0:
            price = self._price(symbol)
        side = "long" if signed > 0 else "short"
        entry = float(pos.get("entryPrice") or 0)
        pnl = (price - entry) * contracts if side == "long" else (entry - price) * contracts
        pnl_pct = (pnl / (entry * contracts)) * 100 if entry and contracts else 0.0
        self.store.record_trade(symbol, side, entry, price, contracts, pnl, pnl_pct, reason)
        self.orders.close_all(symbol)
        self._positions.pop(symbol, None)
        self.trailing.pop(symbol, None)
        self.notifier.info(f"[CLOSE:{reason}] {symbol} {side} pnl={pnl:.2f} ({pnl_pct:.1f}%)")

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
            open_pos = self._open_position(symbol, positions)
            if open_pos and open_pos["side"] != signal["side"]:
                self.notifier.info(f"[REVERSE] {symbol}: close {open_pos['side']} before {signal['side']}")
                self._close_position(open_pos["pos"], "reversal")
                positions = self.exchange.fetch_positions()
                open_pos = None
            if self.one_per_symbol and open_pos:
                logger.info("%s skipped for %s: %s position already open", strategy.name, symbol, open_pos["side"])
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
                    level["status"] = "placed"
                    pending += 1
                except Exception as e:
                    self.notifier.alert(f"Grid order failed {symbol}", str(e))

    def _open_position(self, symbol, positions):
        for pos in positions:
            if pos.get("symbol") != symbol:
                continue
            contracts = float(pos.get("contracts") or 0)
            if contracts == 0:
                continue
            return {"side": "LONG" if contracts > 0 else "SHORT", "pos": pos}
        return None

    def _price(self, symbol):
        try:
            return float(self.exchange.fetch_ticker(symbol)["last"])
        except Exception:
            return 0.0

    def _equity(self):
        balance = self.exchange.fetch_balance()
        return float(balance.get("USDT", {}).get("total", 0) or 0)
