import logging
import time

from agent.core.timestamps import order_age_seconds
from agent.core.utils import is_valid_atr

logger = logging.getLogger("trading-agent")


class OrderManager:
    def __init__(self, exchange, risk, config, notifier):
        self.exchange = exchange
        self.risk = risk
        self.cfg = config["execution"]
        self.notifier = notifier
        self._sl_tp = {}

    def _position_side(self, side):
        if self.cfg.get("position_mode", "one-way") == "hedge":
            return "LONG" if side == "buy" else "SHORT"
        return None

    def open_position(self, symbol, signal, equity, atr=None, ticket=None):
        if not is_valid_atr(atr):
            self.notifier.info(f"[SKIP] {symbol}: ATR invalid ({atr}), entry dibatalkan")
            return None
        side = "buy" if signal["side"] == "LONG" else "sell"
        live_price = self._live_price(symbol)
        if live_price <= 0:
            self.notifier.alert(f"Failed to open position {symbol}", "no live price")
            return None
        amount = self.risk.compute_position_size(symbol, live_price, equity, side, atr)
        if amount <= 0:
            return None

        self.risk.enforce_leverage(symbol)
        order_type = self.cfg["order_type"]

        for attempt in range(self.cfg["retry_attempts"]):
            try:
                idem = None
                if ticket is not None:
                    prefix = self.cfg.get("client_order_id_prefix", "tbot") or "tbot"
                    idem = f"{prefix}-{ticket.ticket_id.lower()}"
                if order_type == "limit":
                    price = self._best_book_price(symbol, side) or live_price
                    params = {"postOnly": True, "clientOrderId": idem} if idem else {"postOnly": True}
                    order = self.exchange.create_order(
                        symbol,
                        "limit",
                        side,
                        amount,
                        price=price,
                        params=params,
                    )
                else:
                    order = self.exchange.create_order(
                        symbol, "market", side, amount,
                        params={"clientOrderId": idem} if idem else None,
                    )
                fill_price = self._wait_fill(symbol, order)
                if fill_price is None:
                    self.cancel_pending(symbol)
                    self.notifier.info(f"[SKIP] {symbol} entry not filled in time, cancelled")
                    return None
                conf = signal.get("confidence", 0) or 0
                conf_disp = conf * 100.0 if 0 <= conf <= 1 else conf
                self.notifier.info(
                    f"[OPEN] {signal['strategy']} {symbol} {signal['side']} "
                    f"amt={amount:.4f} @ {fill_price} conf={conf_disp:.1f}"
                )
                self.place_sl_tp(symbol, order, fill_price, side, amount, atr)
                sl_tp = self._sl_tp.get(symbol, {})
                leverage = min(
                    self.risk.cfg.get("leverage", 1),
                    self.risk.cfg.get("max_leverage", 1),
                )
                self.notifier.send_open(
                    symbol,
                    signal["side"],
                    fill_price,
                    amount,
                    sl=sl_tp.get("sl"),
                    tp=sl_tp.get("tp"),
                    strategy=signal.get("strategy"),
                    leverage=leverage,
                    equity=equity,
                )
                if ticket is not None:
                    order_id = order.get("id")
                    info = order.get("info") or {}
                    exchange_order_id = info.get("orderId") or order.get("clientOrderId")
                    ticket.mark_filled(order_id, exchange_order_id, "market" if order_type == "market" else "limit")
                return order
            except Exception as e:
                logger.warning("open retry %s/%s failed: %s", attempt + 1, self.cfg["retry_attempts"], e)
                time.sleep(1)
        self.notifier.alert(f"Failed to open position {symbol}", "")
        return None

    def _live_price(self, symbol):
        try:
            return float(self.exchange.fetch_ticker(symbol)["last"] or 0)
        except Exception:
            return 0.0

    def _best_book_price(self, symbol, side):
        try:
            book = self.exchange.fetch_order_book(symbol, 1)
            if not book:
                return None
            if side == "buy" and book.get("bids"):
                return float(book["bids"][0][0])
            if side == "sell" and book.get("asks"):
                return float(book["asks"][0][0])
        except Exception:
            pass
        return None

    _FILLED = ("filled", "closed")

    def _wait_fill(self, symbol, order):
        status = order.get("status")
        if status in ("canceled", "rejected", "expired"):
            return None
        avg = order.get("average") or order.get("price")
        if status in self._FILLED:
            return float(avg) if avg else self._live_price(symbol)
        if self.cfg["order_type"] == "market":
            try:
                fetched = self.exchange.fetch_order(order["id"], symbol)
            except Exception:
                fetched = None
            f = fetched or order
            avg = f.get("average") or f.get("price")
            if f.get("status") in self._FILLED:
                return float(avg) if avg else self._live_price(symbol)
            if f.get("status") in ("canceled", "rejected", "expired"):
                return None
            return self._live_price(symbol)
        ttl = self.cfg.get("entry_ttl_seconds", 180)
        deadline = time.time() + ttl
        order_id = order.get("id")
        last_seen = status
        while time.time() < deadline:
            try:
                f = self.exchange.fetch_order(order_id, symbol)
            except Exception:
                f = None
            if f is None:
                try:
                    open_ids = {o.get("id") for o in (self.exchange.fetch_open_orders(symbol) or [])}
                    if order_id not in open_ids:
                        f = self.exchange.fetch_order(order_id, symbol)
                except Exception:
                    f = None
            if f is None:
                f = order
            status = f.get("status")
            if status != last_seen:
                logger.info("[FILL] %s order %s status -> %s", symbol, order_id, status)
                last_seen = status
            if status in self._FILLED:
                avg = f.get("average") or f.get("price")
                return float(avg) if avg else self._live_price(symbol)
            if status in ("canceled", "rejected", "expired"):
                return None
            time.sleep(2)
        return None

    def place_sl_tp(self, symbol, order, entry_price, side, amount, atr=None):
        try:
            if not self.cfg["reduce_only_on_close"]:
                return
            atr = self._fetch_atr(symbol, atr)
            tp_price = self.risk.build_take_profit(entry_price, side, atr)
            sl_price = self.risk.build_stop_loss(entry_price, side, atr)
            if tp_price is None or sl_price is None:
                self.notifier.alert(f"Failed to place SL/TP {symbol}", "invalid ATR")
                return
            self._sl_tp[symbol] = {"sl": sl_price, "tp": tp_price}
            tp_side = "sell" if side == "buy" else "buy"
            self._place_reduce_only(symbol, "limit", tp_side, amount, price=tp_price)
            self._place_stop(symbol, tp_side, amount, sl_price)
        except Exception as e:
            self.notifier.alert(f"Failed to place SL/TP {symbol}", str(e))

    def _reduce_only_params(self, side, extra=None):
        params = {"reduceOnly": True}
        ps = self._position_side(side)
        if ps:
            params["positionSide"] = ps
        if extra:
            params.update(extra)
        return params

    def _place_reduce_only(self, symbol, order_type, side, amount, price=None, extra=None):
        return self.exchange.create_order(
            symbol,
            order_type,
            side,
            amount,
            price=price,
            params=self._reduce_only_params(side, extra),
        )

    def _fetch_atr(self, symbol, atr=None):
        """ATR for the symbol; falls back to a live OHLCV fetch so screener
        symbols outside ``config.symbols`` still get SL/TP protection."""
        if is_valid_atr(atr):
            return atr
        try:
            from agent.core.utils import compute_atr, ohlcv_to_dataframe

            tf = "1h"
            ohlcv = self.exchange.fetch_ohlcv(symbol, tf, limit=100)
            df = ohlcv_to_dataframe(ohlcv)
            period = self.risk.cfg.get("atr_period", 14)
            value = float(compute_atr(df, period).iloc[-1])
            return value if is_valid_atr(value) else None
        except Exception:
            return None

    def _place_stop(self, symbol, side, amount, sl_price):
        try:
            return self.exchange.create_stop_order(symbol, side, sl_price, amount=amount)
        except Exception:
            try:
                return self.exchange.create_order(
                    symbol,
                    "limit",
                    side,
                    amount,
                    price=sl_price,
                    params=self._reduce_only_params(side, {"stopLossPrice": sl_price}),
                )
            except Exception:
                return None

    def close_all(self, symbol):
        positions = self.exchange.fetch_positions([symbol])
        for pos in positions:
            contracts = abs(float(pos.get("contracts") or 0))
            if contracts == 0:
                continue
            pside = str(pos.get("side") or "").lower()
            if pside not in ("long", "short"):
                amt = (pos.get("info") or {}).get("positionAmt")
                pside = "long" if (float(amt or 0) > 0) else "short"
            side = "sell" if pside == "long" else "buy"
            try:
                self.exchange.create_order(
                    symbol,
                    "market",
                    side,
                    abs(contracts),
                    params=self._reduce_only_params(side),
                )
            except Exception:
                try:
                    side_param = "LONG" if pside == "long" else "SHORT"
                    self.exchange.create_order(
                        symbol,
                        "market",
                        side,
                        contracts,
                        params={"reduceOnly": True, "positionSide": side_param},
                    )
                except Exception as e:
                    self.notifier.alert(f"Failed to close {symbol}", str(e))
                    continue
            self.notifier.info(f"[CLOSE] {symbol} {side} {contracts}")

    def cancel_pending(self, symbol=None):
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            for o in orders:
                self.exchange.cancel_order(o["id"], o["symbol"])
            return orders
        except Exception as e:
            logger.warning("cancel pending failed: %s", e)
            return []

    def cancel_stale_orders(self, ttl=300, symbol=None):
        if ttl <= 0:
            return
        try:
            orders = self.exchange.fetch_open_orders(symbol)
        except Exception:
            return
        for o in orders:
            # reduce-only SL/TP guards are protection, never auto-cancel them.
            if o.get("reduceOnly"):
                continue
            if not o.get("id") or not o.get("symbol"):
                continue
            ts = o.get("timestamp")
            age = order_age_seconds(ts)
            if ts and age > ttl:
                try:
                    self.exchange.cancel_order(o["id"], o["symbol"])
                    self.notifier.info(f"[CANCEL-STALE] {o['symbol']} age={age/60:.1f}m")
                except Exception as e:
                    logger.warning("cancel stale %s failed: %s", o["id"], e)
        self._cancel_stale_stops(ttl, symbol)

    def _cancel_stale_stops(self, ttl, symbol=None) -> None:
        """Cancel orphaned conditional (algo) stop orders.

        Algo stops live outside ``fetch_open_orders`` and are never seen by the
        regular stale sweep; if a position is closed/reversed while a stop is
        still triggered-pending it can fire against a new opposite position.
        """
        try:
            stops = self.exchange.fetch_open_stop_orders(symbol) or []
        except Exception:
            return
        for s in stops:
            ts = s.get("timestamp") or s.get("triggerTime") or s.get("updateTime")
            age = order_age_seconds(ts)
            if ts and age > ttl:
                try:
                    self.exchange.cancel_stop_order(s.get("algoId") or s.get("id"), s.get("symbol") or symbol)
                    self.notifier.info(f"[CANCEL-STALE-STOP] {(s.get('symbol') or symbol)} age={age/60:.1f}m")
                except Exception as e:
                    logger.warning("cancel stale stop %s failed: %s", s.get("id"), e)

    def _open_algo_orders(self, symbol):
        try:
            s = symbol.replace("/USDT:USDT", "USDT")
            result = self.exchange.client.fapiPrivateGetOpenAlgoOrders({"symbol": s})
            if isinstance(result, dict):
                return result.get("orders") or []
            return result or []
        except Exception:
            return []

    def ensure_sl_tp(self, symbol, side, entry, amount, atr=None):
        if not self.cfg["reduce_only_on_close"]:
            return
        try:
            atr = self._fetch_atr(symbol, atr)
            tp_price = self.risk.build_take_profit(entry, side, atr)
            sl_price = self.risk.build_stop_loss(entry, side, atr)
            if tp_price is None or sl_price is None:
                saved = self._sl_tp.get(symbol)
                if not saved:
                    return
                tp_price, sl_price = saved["tp"], saved["sl"]
            tp_side = "sell" if side == "buy" else "buy"
            algo = self._open_algo_orders(symbol)
            has_sl = any(
                (o.get("orderType") or "").startswith("STOP")
                and str(o.get("algoStatus") or "").upper() not in ("CANCELED", "EXPIRED")
                for o in algo
            )
            if not has_sl:
                self._place_stop(symbol, tp_side, amount, sl_price)
                self.notifier.info(f"[SL-GUARD] pasang ulang SL {symbol} @ {sl_price}")
            reg = self.exchange.fetch_open_orders(symbol) or []
            has_tp = any(
                o.get("reduceOnly") and str(o.get("side") or "").lower() == tp_side for o in reg
            )
            if not has_tp:
                self._place_reduce_only(symbol, "limit", tp_side, amount, price=tp_price)
                self.notifier.info(f"[TP-GUARD] pasang ulang TP {symbol} @ {tp_price}")
        except Exception as e:
            if "ReduceOnly" in str(e) or "-2022" in str(e):
                logger.info("SL/TP guard skipped for %s: position no longer open", symbol)
                return
            self.notifier.alert(f"Guard SL/TP gagal {symbol}", str(e))
