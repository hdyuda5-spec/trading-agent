"""Portfolio service — owns all position/equity state and trade lifecycle.

Business logic extracted from the old ``TradingBot``: equity baseline + top-up
detection, daily-loss halt, trailing stops, external-close reconciliation,
SL/TP guarding, position close + PnL recording and post-trade reflection.

The service is stateful but dependency-injected: it never touches the exchange
through anything other than the injected ``exchange`` and never places orders
itself beyond delegating to ``orders``. It publishes ``equity.updated``,
``positions.updated`` and ``trade.closed`` events on the bus.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from agent.core.utils import compute_atr, is_valid_atr
from agent.services.event_bus import Event, EventBus

logger = logging.getLogger("trading-agent")


class PortfolioService:
    def __init__(self, config, exchange, risk, orders, store, notifier, reflector, candles, bus: EventBus):
        self.config = config
        self.exchange = exchange
        self.risk = risk
        self.orders = orders
        self.store = store
        self.notifier = notifier
        self.reflector = reflector
        self.candles = candles
        self.bus = bus

        self.halted = False
        self.trailing: Dict[str, dict] = store.load_state("trailing", {}) or {}
        self._positions: Dict[str, dict] = {}
        self._closed_at: Dict[str, float] = {}
        self._position_strategy: Dict[str, str] = {}
        self._position_setup: Dict[str, dict] = {}
        self._position_confidence: Dict[str, float] = {}
        self._last_tick = time.time()
        self._lock = threading.RLock()

    # -- equity ----------------------------------------------------------

    @property
    def initial_equity(self):
        return self.risk.drawdown.initial_equity

    @property
    def peak_equity(self):
        return self.risk.drawdown.peak_equity

    def equity(self) -> float:
        balance = self.exchange.fetch_balance()
        return float(balance.get("USDT", {}).get("total", 0) or 0)

    def ensure_equity_baseline(self, equity: float) -> None:
        """Daily baseline + top-up detection (was spread across bot.tick)."""
        baseline = self.store.load_state("equity_baseline", None)
        today = time.strftime("%Y-%m-%d")
        if not baseline or baseline.get("date") != today:
            self.store.save_state("equity_baseline", {"date": today, "equity": equity})
            self.risk.set_initial_equity(equity)
        elif baseline.get("equity", 0) > 0 and equity > baseline["equity"] * 1.20:
            self.store.save_state("equity_baseline", {"date": today, "equity": equity})
            self.risk.set_initial_equity(equity)
            self.notifier.info(
                f"Equity top-up terdeteksi: baseline direset "
                f"{baseline['equity']:.2f} -> {equity:.2f} USDT"
            )
        if not self.risk.drawdown.initial_equity:
            if baseline and baseline.get("date") == today and baseline.get("equity", 0) > 0:
                self.risk.set_initial_equity(baseline["equity"])
            elif equity > 0:
                self.risk.set_initial_equity(equity)
                self.notifier.info(f"Initial equity set: {equity:.2f} USDT")
        self.risk.update_equity(equity)

    def refresh_equity(self) -> float:
        equity = self.equity()
        self.risk.update_equity(equity)
        self.bus.publish_sync(Event("equity.updated", {"equity": equity}, source="portfolio"))
        return equity

    # -- positions -------------------------------------------------------

    def positions(self) -> List[dict]:
        try:
            return self.exchange.fetch_positions()
        except Exception:
            return []

    def pos_side(self, pos) -> str:
        side = str(pos.get("side") or "").lower()
        if side in ("long", "short"):
            return side
        amt = (pos.get("info") or {}).get("positionAmt")
        if amt is not None:
            return "long" if float(amt) > 0 else "short"
        return "long" if float(pos.get("contracts") or 0) > 0 else "short"

    def open_position(self, symbol: str, positions) -> Optional[dict]:
        for pos in positions:
            if pos.get("symbol") != symbol:
                continue
            if abs(float(pos.get("contracts") or 0)) == 0:
                continue
            side = self.pos_side(pos)
            return {"side": "LONG" if side == "long" else "SHORT", "pos": pos}
        return None

    def set_trade_meta(self, symbol: str, strategy: str, setup: dict, confidence: float) -> None:
        self._position_strategy[symbol] = strategy
        self._position_setup[symbol] = setup
        self._position_confidence[symbol] = confidence

    def pop_trade_meta(self, symbol: str) -> tuple:
        strategy = self._position_strategy.pop(symbol, "bot")
        setup = self._position_setup.pop(symbol, {}) or {}
        if setup.get("confidence") is None:
            setup["confidence"] = self._position_confidence.pop(symbol, None)
        else:
            self._position_confidence.pop(symbol, None)
        return strategy, setup

    def capture_setup(self, symbol, side, entry, atr, metadata=None, confidence=None) -> dict:
        return {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "atr": atr,
            "confidence": confidence,
            "metadata": metadata or {},
        }

    def atr_of(self, df) -> Optional[float]:
        if df is None:
            return None
        try:
            atr = float(compute_atr(df, self.config["risk"].get("atr_period", 14)).iloc[-1])
        except Exception:
            return None
        return atr if is_valid_atr(atr) else None

    # -- lifecycle: manage, guard, close ---------------------------------

    def manage(self, equity: float, positions: List[dict]) -> None:
        """Daily-loss halt, trailing stops, external-close reconciliation."""
        self._last_tick = time.time()
        if self.risk.daily_loss_exceeded(equity):
            self.notifier.alert("Daily loss limit reached, closing all positions", "")
            seen = set()
            for pos in positions:
                symbol = pos.get("symbol")
                if not symbol or symbol in seen or abs(float(pos.get("contracts") or 0)) == 0:
                    continue
                seen.add(symbol)
                self.close_position(pos, "daily_loss")
            self.halted = True
            return
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            tickers = {}
        for pos in positions:
            contracts = abs(float(pos.get("contracts") or 0))
            if contracts == 0 or not pos.get("symbol"):
                continue
            symbol = pos["symbol"]
            side = self.pos_side(pos)
            last = float(tickers.get(symbol, {}).get("last") or 0)
            if last <= 0:
                continue
            entry = float(pos.get("entryPrice") or 0)
            peak = self.trailing.get(symbol, {}).get("peak")
            if peak is None or (side == "long" and last > peak) or (side == "short" and last < peak):
                peak = last
            self.trailing[symbol] = {"side": side, "peak": peak, "entry": entry}
            atr = self.atr_of(self.candles.get(symbol))
            if self.risk.trailing_stop_hit(side, peak, last, atr):
                self.notifier.info(f"[TRAILING] closing {symbol} {side} at {last}")
                self.close_position(pos, "trailing", last)
        self._detect_external_closes(positions)
        now = time.time()
        self._positions = {
            p.get("symbol"): p
            for p in positions
            if abs(float(p.get("contracts") or 0)) > 0
            and now - self._closed_at.get(p.get("symbol"), 0) >= 1800
        }
        self.bus.publish_sync(Event("positions.updated", {"count": len(self._positions)}, source="portfolio"))

    def _detect_external_closes(self, positions: List[dict]) -> None:
        current = {p.get("symbol") for p in positions if abs(float(p.get("contracts") or 0)) > 0}
        for symbol, old in list(self._positions.items()):
            if symbol in current:
                continue
            if time.time() - self._closed_at.get(symbol, 0) < 1800:
                continue
            self._positions.pop(symbol, None)
            self.close_position(old, "sl_tp")

    def guard_sl_tp(self) -> None:
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
            side = "buy" if self.pos_side(pos) == "long" else "sell"
            entry = float(pos.get("entryPrice") or 0)
            if entry <= 0:
                continue
            atr = self.atr_of(self.candles.get(symbol))
            self.orders.ensure_sl_tp(symbol, side, entry, contracts, atr)

    def reconcile_stale_trailing(self) -> None:
        try:
            positions = self.exchange.fetch_positions()
        except Exception:
            return
        open_symbols = {
            p.get("symbol")
            for p in positions
            if abs(float(p.get("contracts") or 0)) > 0 and p.get("symbol")
        }
        for symbol in list(self.trailing.keys()):
            if symbol in open_symbols:
                continue
            info = self.trailing.pop(symbol, None)
            if not info:
                continue
            side = info.get("side")
            entry = float(info.get("entry") or 0)
            exit_px = self._last_close_price(symbol) or self._price(symbol)
            if side and entry > 0 and exit_px > 0:
                contracts = self._recent_contracts(symbol, side)
                if contracts > 0:
                    pnl = (exit_px - entry) * contracts if side == "long" else (entry - exit_px) * contracts
                    pnl_pct = (pnl / (entry * contracts)) * 100
                    strategy, setup = self.pop_trade_meta(symbol)
                    self.store.record_trade(symbol, side, entry, exit_px, contracts, pnl, pnl_pct, "sl_tp", strategy)
                    self.record_experience(symbol, side, strategy, setup, pnl, pnl_pct, "sl_tp")
                    self.notifier.info(f"[RECONCILE] {symbol} {side} closed eksternal: pnl={pnl:.2f} ({pnl_pct:.1f}%)")
            self._position_strategy.pop(symbol, None)
            self._position_setup.pop(symbol, None)

    def close_position(self, pos, reason: str, price: float = 0.0) -> None:
        symbol = pos.get("symbol")
        if not symbol:
            return
        self._closed_at[symbol] = time.time()
        try:
            live = self.exchange.fetch_positions([symbol])
        except Exception:
            live = []
        live_pos = next((p for p in live if abs(float(p.get("contracts") or 0)) > 0), None)
        if live_pos is None:
            self._record_flat_close(pos, reason, price)
            return
        contracts = abs(float(live_pos.get("contracts") or 0))
        if contracts == 0:
            return
        if price <= 0:
            price = self._price(symbol)
        side = self.pos_side(live_pos)
        entry = float(live_pos.get("entryPrice") or 0)
        pnl = (price - entry) * contracts if side == "long" else (entry - price) * contracts
        pnl_pct = (pnl / (entry * contracts)) * 100 if entry and contracts else 0.0
        strategy, setup = self.pop_trade_meta(symbol)
        self.store.record_trade(
            symbol, side, entry, price, contracts, pnl, pnl_pct, reason, strategy, setup.get("confidence")
        )
        self.record_experience(symbol, side, strategy, setup, pnl, pnl_pct, reason)
        self.orders.close_all(symbol)
        self._positions.pop(symbol, None)
        self.trailing.pop(symbol, None)
        self.notifier.info(f"[CLOSE:{reason}] {symbol} {side} pnl={pnl:.2f} ({pnl_pct:.1f}%)")
        self.notifier.send_close(symbol, side, entry, price, contracts, pnl, pnl_pct, reason, strategy)
        self.bus.publish_sync(Event("trade.closed", {"symbol": symbol, "pnl": pnl, "reason": reason}, source="portfolio"))

    def _record_flat_close(self, pos, reason: str, price: float) -> None:
        symbol = pos.get("symbol")
        contracts = abs(float(pos.get("contracts") or 0))
        entry = float(pos.get("entryPrice") or 0)
        side = self.pos_side(pos)
        if contracts > 0 and entry > 0:
            if price <= 0:
                price = (
                    self._fill_exit_price(symbol, side)
                    or self._last_close_price(symbol)
                    or self._price(symbol)
                )
            if price > 0:
                pnl = (price - entry) * contracts if side == "long" else (entry - price) * contracts
                pnl_pct = (pnl / (entry * contracts)) * 100 if entry and contracts else 0.0
                strategy, setup = self.pop_trade_meta(symbol)
                self.store.record_trade(
                    symbol, side, entry, price, contracts, pnl, pnl_pct, reason, strategy, setup.get("confidence")
                )
                self.record_experience(symbol, side, strategy, setup, pnl, pnl_pct, reason)
                self.notifier.info(f"[CLOSE:{reason}] {symbol} {side} pnl={pnl:.2f} ({pnl_pct:.1f}%)")
                self.notifier.send_close(symbol, side, entry, price, contracts, pnl, pnl_pct, reason, strategy)
        self._position_strategy.pop(symbol, None)
        self._position_setup.pop(symbol, None)
        self._position_confidence.pop(symbol, None)
        self._positions.pop(symbol, None)
        self.trailing.pop(symbol, None)
        self.bus.publish_sync(Event("trade.closed", {"symbol": symbol, "pnl": pnl if price > 0 else 0.0, "reason": reason}, source="portfolio"))

    # -- experience / learning -------------------------------------------

    def record_experience(self, symbol, side, strategy, setup, pnl, pnl_pct, reason) -> None:
        outcome = "win" if pnl >= 0 else "loss"
        lesson = self._build_lesson(symbol, side, strategy, setup, pnl, pnl_pct, reason)
        exp_id = self.store.add_experience(symbol, side, strategy, setup, outcome, reason, pnl, pnl_pct, lesson)
        self.reflector.reflect_async(exp_id, symbol, side, strategy, setup, pnl, pnl_pct, reason)

    def _build_lesson(self, symbol, side, strategy, setup, pnl, pnl_pct, reason) -> str:
        meta = setup.get("metadata") or {}
        rsi = meta.get("rsi")
        rsi_txt = f", rsi={rsi:.1f}" if rsi is not None else ""
        entry = setup.get("entry")
        entry_txt = f", entry={entry}" if entry else ""
        conf = setup.get("confidence")
        if conf is not None:
            display = conf * 100.0 if 0 <= conf <= 1 else conf
            conf_txt = f", conf={display:.0f}"
        else:
            conf_txt = ""
        base = f"{symbol} {side.upper()} via {strategy}: {pnl_pct:+.1f}% ({reason}){entry_txt}{rsi_txt}{conf_txt}"
        if pnl >= 0:
            return f"Setup BERHASIL: {base}. Pertahankan pola seperti ini."
        return f"Setup GAGAL: {base}. Jangan ulangi pola yang gagal; tunggu konfirmasi lebih kuat."

    # -- price helpers ---------------------------------------------------

    def _price(self, symbol) -> float:
        try:
            return float(self.exchange.fetch_ticker(symbol)["last"])
        except Exception:
            return 0.0

    def _last_close_price(self, symbol) -> float:
        try:
            trades = self.exchange.fetch_my_trades(symbol, limit=5) or []
        except Exception:
            return 0.0
        for t in trades:
            try:
                return float(t.get("price") or 0)
            except Exception:
                continue
        return 0.0

    def _fill_exit_price(self, symbol, pos_side, window_s=3600) -> float:
        try:
            trades = self.exchange.fetch_my_trades(symbol, limit=50) or []
        except Exception:
            return 0.0
        want = "buy" if pos_side == "long" else "sell"
        cutoff = time.time() * 1000 - window_s * 1000
        for t in trades:
            if (t.get("timestamp") or 0) < cutoff:
                continue
            if str(t.get("side") or "").lower() == want:
                return float(t.get("price") or 0)
        return 0.0

    def _recent_contracts(self, symbol, side) -> float:
        try:
            trades = self.exchange.fetch_my_trades(symbol, limit=5) or []
        except Exception:
            return 0.0
        for t in trades:
            if str(t.get("side") or "").lower() == ("sell" if side == "long" else "buy"):
                return abs(float(t.get("amount") or 0))
        return 0.0
