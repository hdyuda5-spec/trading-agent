"""TradingBot — orchestrator only.

The old TradingBot was an 800-line monolith that fetched data, computed
indicators, sized positions, placed orders, guarded SL/TP, screened the market
and reported — all inside one class. All of that business logic now lives in
the service layer (``agent.services``). This class only wires components and
drives the event loop:

    WebSocket feed -> EventBus -> CandleStore -> FeatureEngine
        -> strategies/DecisionEngine -> RiskEngine -> ExecutionService
        -> PortfolioService

Execution model: no polling for market data. The feed pushes ``market.tick``
and ``market.candle`` events onto the bus; a closed candle triggers strategy
evaluation. A lightweight async management loop refreshes equity/positions,
SL/TP guards and scheduled jobs (screen/whale/report) — the only polling in
the system is the REST fallback inside ``MarketFeed`` when the socket dies.

Backward compatibility: the attributes and helpers the TelegramController and
``main.py`` rely on are preserved as thin shims (``_equity``, ``_pos_side``,
``initial_equity``, ``trailing``, ``_dfs``, ``tick``, ...).
"""

import asyncio
import logging
import threading
import time

from agent.core.exchange import ExchangeClient
from agent.core.reflector import Reflector
from agent.core.risk import RiskEngine
from agent.core.trend import TrendFilter
from agent.core.whale import WhaleDetector
from agent.data.trades import TradeStore
from agent.decision.engine import UnifiedDecisionEngine
from agent.decision.reviewer import MissedTradeJournal, TradeReviewer
from agent.decision.telemetry import SignalFunnel
from agent.execution.notifier import Notifier
from agent.execution.order import OrderManager
from agent.execution.paper import PaperExchange
from agent.execution.telegram_ctl import TelegramController
from agent.features import build_feature_engine
from agent.services import (
    CandleStore,
    EventBus,
    ExecutionService,
    MarketFeed,
    PortfolioService,
    ReportingService,
    ScreenerService,
    SignalService,
    WhaleService,
)
from agent.strategies import build_strategies

logger = logging.getLogger("trading-agent")


class TradingBot:
    """Wires the services into the async event-driven pipeline and runs it."""

    def __init__(self, config):
        self.config = config
        self.notifier = Notifier(config.get("telegram", {}))
        self.exchange = ExchangeClient(
            name=config["exchange"]["name"],
            testnet=config["exchange"].get("testnet", True),
            options=config["exchange"].get("options", {}),
        )
        # Trading mode: default PAPER — safe start even if balances/keys are
        # present. `trading.mode: "live"` must be set explicitly to touch real
        # funds, and even then `screener.auto_trade` stays false.
        mode = str(config.get("trading", {}).get("mode", "paper")).lower()
        if mode == "paper":
            paper_cfg = config.get("trading", {}).get("paper", {}) or {}
            initial = float(paper_cfg.get("initial_balance_usdt", 1000.0))
            fee = float(paper_cfg.get("fee_pct", 0.05))
            self.paper = PaperExchange(self.exchange, initial_balance_usdt=initial, fee_pct=fee)
            self.exchange = self.paper
            self.notifier.info(
                f"PAPER MODE active (initial {initial:.0f} USDT, fee {fee:.3f}%). "
                f"Set trading.mode=\"live\" + screener.auto_trade=true to go live."
            )
        else:
            self.paper = None
            if mode != "live":
                raise ValueError(f"trading.mode harus 'paper' atau 'live', dapat {mode!r}")

        # engines
        self.risk = RiskEngine(config["risk"], self.exchange)
        self.orders = OrderManager(self.exchange, self.risk, config, self.notifier)
        self.feature_engine = build_feature_engine(self.exchange, config)
        self.strategies = build_strategies(
            config, self.exchange, self.notifier, feature_engine=self.feature_engine
        )
        self.trend = TrendFilter(self.exchange, config)
        self.store = TradeStore()
        self.decision_engine = UnifiedDecisionEngine(
            config, feature_engine=self.feature_engine, risk=self.risk,
            portfolio=self, exchange=self.exchange,
        )
        self.funnel = SignalFunnel(store=self.store)
        self.missed_journal = MissedTradeJournal(store=self.store)
        self.reviewer = TradeReviewer(store=self.store)
        self.reflector = Reflector(
            config,
            config.get("strategies", {}).get("ai_signal", {}),
            self.notifier,
            self.store,
        )

        # backbone
        self.bus = EventBus()
        self.candles = CandleStore()

        # services (business logic lives here)
        self.portfolio = PortfolioService(
            config, self.exchange, self.risk, self.orders, self.store,
            self.notifier, self.reflector, self.candles, self.bus,
        )
        self.execution = ExecutionService(
            config, self.exchange, self.risk, self.orders, self.notifier,
            self.strategies, self._whale_detector(), self.portfolio,
            store=self.store, funnel=self.funnel,
        )
        self.signals = SignalService(
            config, self.exchange, self.notifier, self.risk, self.execution,
            self.feature_engine, self.strategies, self.trend, self._whale_detector(),
            self.portfolio, self.candles,
            store=self.store, decision_engine=self.decision_engine,
            funnel=self.funnel, missed_journal=self.missed_journal,
        )
        self.screen = ScreenerService(
            config, self.exchange, self.notifier, self.risk, self.execution,
            self.feature_engine, self._whale_detector(), self.portfolio, self.store,
            self.candles,
            decision_engine=self.decision_engine, funnel=self.funnel,
            missed_journal=self.missed_journal,
        )
        self.whale_svc = WhaleService(config, self.exchange, self.notifier, self._whale_detector(), self.bus)

        # legacy attributes kept for TelegramController / tooling
        self.telegram = TelegramController(self)
        self.interval = int(config["execution"]["poll_interval_seconds"])
        self.one_per_symbol = config["execution"].get("one_position_per_symbol", True)
        self.report_hour = int(config.get("reporting", {}).get("daily_hour", 8))
        self.reporting = ReportingService(config, self.telegram, self.portfolio)
        self._min_equity = float(config["risk"].get("min_equity_usdt", 20))
        self._low_balance_halted = False
        self._low_equity_notified = False
        self._start_time = time.time()
        self._trade_lock = threading.Lock()
        self._last_prices = {}
        self._running = False

    # -- compatibility shims ---------------------------------------------

    def _whale_detector(self):
        if not hasattr(self, "_whale"):
            self._whale = WhaleDetector(self.exchange, self.config, self.notifier)
        return self._whale

    @property
    def whale(self):
        return self._whale_detector()

    @property
    def halted(self):
        return self.portfolio.halted

    @halted.setter
    def halted(self, value):
        self.portfolio.halted = value

    @property
    def initial_equity(self):
        return self.portfolio.initial_equity

    @property
    def trailing(self):
        return self.portfolio.trailing

    @property
    def _positions(self):
        return self.portfolio._positions

    @property
    def _closed_at(self):
        return self.portfolio._closed_at

    @property
    def _position_strategy(self):
        return self.portfolio._position_strategy

    @property
    def _position_setup(self):
        return self.portfolio._position_setup

    @property
    def _position_confidence(self):
        return self.portfolio._position_confidence

    @property
    def _dfs(self):
        return self.candles.all()

    @property
    def _last_tick(self):
        return self.portfolio._last_tick

    def _equity(self):
        return self.portfolio.equity()

    def _pos_side(self, pos):
        return self.portfolio.pos_side(pos)

    def _open_position(self, symbol, positions):
        return self.portfolio.open_position(symbol, positions)

    def _close_position(self, pos, reason, price=0.0):
        return self.portfolio.close_position(pos, reason, price)

    # -- entrypoint ------------------------------------------------------

    def run(self):
        if not self.exchange.check_health():
            self.notifier.alert("Exchange unreachable, aborting", "")
            return
        self.notifier.info(
            f"Agent started on {self.config['exchange']['name']} "
            f"(testnet={self.exchange.testnet})"
        )
        try:
            asyncio.run(self._async_run())
        except KeyboardInterrupt:
            logger.info("Orchestrator stopped by user")

    def tick(self):
        """Synchronous management pass (kept for tooling / fallback)."""
        self._manage_once_sync()

    # -- async orchestration ---------------------------------------------

    async def _async_run(self):
        loop = asyncio.get_running_loop()
        self.bus.bind(loop)
        self._running = True
        self.telegram.start()

        equity = await asyncio.to_thread(self.portfolio.equity)
        self.portfolio.ensure_equity_baseline(equity)
        await asyncio.to_thread(self.portfolio.reconcile_stale_trailing)
        await self._seed_candles()

        self._subscribe()
        logger.info(
            "Orchestrator online: %d symbols @ %s, %d strategies",
            len(self.config["symbols"]),
            self.config["timeframe"],
            len(self.strategies),
        )

        feed = MarketFeed(self.exchange, self.config, self.bus)
        tasks = [
            asyncio.create_task(feed.start(), name="market-feed"),
            asyncio.create_task(self._management_loop(), name="management"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            self._running = False
            await feed.stop()
            for task in tasks:
                task.cancel()

    def _subscribe(self) -> None:
        self.bus.subscribe("market.candle", self._on_market_candle)
        self.bus.subscribe("market.tick", self._on_market_tick)

    async def _on_market_candle(self, event):
        self.candles.apply_candle_event(event)
        closed = (event.data.get("candle") or {}).get("closed", False)
        if closed and event.data.get("symbol") in self.config["symbols"]:
            await asyncio.to_thread(self.evaluate_symbol, event.data["symbol"])

    async def _on_market_tick(self, event):
        self._last_prices[event.data["symbol"]] = event.data["price"]

    async def _seed_candles(self) -> None:
        symbols = self.config["symbols"]
        timeframe = self.config["timeframe"]
        for symbol in symbols:
            try:
                ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, timeframe, 200)
                self.candles.seed(symbol, ohlcv)
                logger.info("CandleStore seeded %s (%d bars)", symbol, len(ohlcv or []))
            except Exception as e:
                logger.warning("CandleStore seed %s failed: %s", symbol, e)

    # -- strategy evaluation (event-driven) ------------------------------

    def evaluate_symbol(self, symbol: str) -> None:
        df = self.candles.get(symbol)
        if df is None or len(df) < 5:
            return
        try:
            equity = self.portfolio.equity()
            positions = self.portfolio.positions()
            atr = self.portfolio.atr_of(df)
            self.signals.evaluate(symbol, df, positions, equity, atr)
        except Exception as e:
            self.notifier.alert(f"Signal eval {symbol} gagal", str(e))

    # -- management loop --------------------------------------------------

    async def _management_loop(self) -> None:
        while self._running:
            try:
                await self._manage_once_async()
            except Exception as e:
                self.notifier.alert("Tick error", str(e))
            await asyncio.sleep(self.interval)

    async def _manage_once_async(self) -> None:
        equity = await asyncio.to_thread(self.portfolio.equity)
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
        self.portfolio.ensure_equity_baseline(equity)
        positions = await asyncio.to_thread(self.portfolio.positions)
        await asyncio.to_thread(self.portfolio.manage, equity, positions)
        if self.portfolio.halted:
            return
        await asyncio.to_thread(self.portfolio.guard_sl_tp)
        await asyncio.to_thread(
            self.execution.cancel_stale_orders,
            self.config["execution"].get("order_ttl_seconds", 0),
        )
        min_eq = self._min_equity
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
        for symbol in self.config["symbols"]:
            df = self.candles.get(symbol)
            atr_period = self.config["risk"].get("atr_period", 14)
            if df is None or len(df) < atr_period + 2:
                continue
            atr = self.portfolio.atr_of(df)
            await asyncio.to_thread(self.execution.run_grid, symbol, positions, equity, atr)
        await asyncio.to_thread(self.store.save_state, "trailing", self.portfolio.trailing_snapshot())
        try:
            await asyncio.to_thread(self.funnel.save)
        except Exception as e:
            logger.warning("funnel save failed: %s", e)
        await self._schedule_jobs()

    def _manage_once_sync(self) -> None:
        """Synchronous variant of ``_manage_once_async`` (fallback/tooling)."""
        equity = self.portfolio.equity()
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
        self.portfolio.ensure_equity_baseline(equity)
        positions = self.portfolio.positions()
        self.portfolio.manage(equity, positions)
        if self.portfolio.halted:
            return
        self.portfolio.guard_sl_tp()
        self.execution.cancel_stale_orders(self.config["execution"].get("order_ttl_seconds", 0))
        min_eq = self._min_equity
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
        for symbol in self.config["symbols"]:
            df = self.candles.get(symbol)
            atr_period = self.config["risk"].get("atr_period", 14)
            if df is None or len(df) < atr_period + 2:
                continue
            atr = self.portfolio.atr_of(df)
            self.execution.run_grid(symbol, positions, equity, atr)
        self.store.save_state("trailing", self.portfolio.trailing_snapshot())
        try:
            self.funnel.save()
        except Exception as e:
            logger.warning("funnel save failed: %s", e)
        self._schedule_jobs_sync()

    async def _schedule_jobs(self) -> None:
        if self.screen.should_run():
            asyncio.create_task(asyncio.to_thread(self.screen.run), name="auto-screen")
        if self.whale_svc.should_scan():
            asyncio.create_task(asyncio.to_thread(self.whale_svc.run), name="whale-scan")
        if self.reporting.should_report():
            asyncio.create_task(asyncio.to_thread(self.reporting.run), name="daily-report")

    def _schedule_jobs_sync(self) -> None:
        if self.screen.should_run():
            threading.Thread(target=self.screen.run, daemon=True).start()
        if self.whale_svc.should_scan():
            threading.Thread(target=self.whale_svc.run, daemon=True).start()
        if self.reporting.should_report():
            self.reporting.run()

    def _run_auto_screen(self):
        self.screen.run()

    def _run_whale_scan(self):
        self.whale_svc.run()
