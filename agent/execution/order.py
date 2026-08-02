import logging
import time

logger = logging.getLogger("trading-agent")


class OrderManager:
    def __init__(self, exchange, risk, config, notifier):
        self.exchange = exchange
        self.risk = risk
        self.cfg = config["execution"]
        self.notifier = notifier

    def _position_side(self, side):
        if self.cfg.get("position_mode", "one-way") == "hedge":
            return "LONG" if side == "buy" else "SHORT"
        return None

    def open_position(self, symbol, signal, equity, atr=None):
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
                if order_type == "limit":
                    order = self.exchange.create_order(
                        symbol,
                        "limit",
                        side,
                        amount,
                        price=live_price,
                        params={"postOnly": True},
                    )
                else:
                    order = self.exchange.create_order(symbol, "market", side, amount)
                fill_price = self._wait_fill(symbol, order)
                if fill_price is None:
                    self.cancel_pending(symbol)
                    self.notifier.info(f"[SKIP] {symbol} entry not filled in time, cancelled")
                    return None
                self.notifier.info(
                    f"[OPEN] {signal['strategy']} {symbol} {signal['side']} "
                    f"amt={amount:.4f} @ {fill_price} conf={signal['confidence']:.1f}"
                )
                self.place_sl_tp(symbol, order, fill_price, side, amount, atr)
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

    def _wait_fill(self, symbol, order):
        status = order.get("status")
        if status in ("canceled", "rejected", "expired"):
            return None
        avg = order.get("average") or order.get("price")
        if status == "filled":
            return float(avg) if avg else self._live_price(symbol)
        if self.cfg["order_type"] == "market":
            try:
                fetched = self.exchange.fetch_order(order["id"], symbol)
            except Exception:
                fetched = None
            f = fetched or order
            avg = f.get("average") or f.get("price")
            if f.get("status") == "filled":
                return float(avg) if avg else self._live_price(symbol)
            if f.get("status") in ("canceled", "rejected", "expired"):
                return None
            return self._live_price(symbol)
        ttl = self.cfg.get("entry_ttl_seconds", 180)
        deadline = time.time() + ttl
        while time.time() < deadline:
            try:
                fetched = self.exchange.fetch_order(order["id"], symbol)
            except Exception:
                fetched = None
            f = fetched or order
            status = f.get("status")
            if status == "filled":
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
            tp_price = self.risk.build_take_profit(entry_price, side, atr)
            sl_price = self.risk.build_stop_loss(entry_price, side, atr)
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

    def _place_stop(self, symbol, side, amount, sl_price):
        try:
            return self.exchange.create_order(
                symbol,
                "stop_market",
                side,
                amount,
                params=self._reduce_only_params(side, {"stopPrice": sl_price}),
            )
        except Exception:
            return self.exchange.create_order(
                symbol,
                "limit",
                side,
                amount,
                price=sl_price,
                params=self._reduce_only_params(side, {"stopLossPrice": sl_price}),
            )

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
        now = time.time()
        for o in orders:
            if o.get("reduceOnly"):
                continue
            ts = o.get("timestamp")
            if not ts:
                continue
            age = (now - ts / 1000.0) * 60 if ts > 1e12 else now - ts
            if age > ttl:
                try:
                    self.exchange.cancel_order(o["id"], o["symbol"])
                    self.notifier.info(f"[CANCEL-STALE] {o['symbol']} age={age/60:.1f}m")
                except Exception as e:
                    logger.warning("cancel stale %s failed: %s", o["id"], e)

    def _open_algo_orders(self, symbol):
        try:
            s = symbol.replace("/USDT:USDT", "USDT")
            return self.exchange.client.fapiPrivateGetOpenAlgoOrders({"symbol": s}) or []
        except Exception:
            return []

    def ensure_sl_tp(self, symbol, side, entry, amount, atr=None):
        if not self.cfg["reduce_only_on_close"]:
            return
        try:
            tp_price = self.risk.build_take_profit(entry, side, atr)
            sl_price = self.risk.build_stop_loss(entry, side, atr)
            tp_side = "sell" if side == "buy" else "buy"
            algo = self._open_algo_orders(symbol)
            has_sl = any(
                (o.get("orderType") or "").startswith("STOP") and float(o.get("quantity") or 0) > 0
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
            self.notifier.alert(f"Guard SL/TP gagal {symbol}", str(e))
