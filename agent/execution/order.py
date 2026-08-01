import logging
import time

logger = logging.getLogger("trading-agent")


class OrderManager:
    def __init__(self, exchange, risk, config, notifier):
        self.exchange = exchange
        self.risk = risk
        self.cfg = config["execution"]
        self.notifier = notifier

    def open_position(self, symbol, signal, equity, atr=None):
        side = "buy" if signal["side"] == "LONG" else "sell"
        ticker = self.exchange.fetch_ticker(symbol)
        price = float(signal.get("price") or ticker["last"])
        amount = self.risk.compute_position_size(symbol, price, equity, side, atr)
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
                        price=price,
                        params={"postOnly": True},
                    )
                else:
                    order = self.exchange.create_order(symbol, "market", side, amount)
                self.notifier.info(
                    f"[OPEN] {signal['strategy']} {symbol} {signal['side']} "
                    f"amt={amount:.4f} @ {price} conf={signal['confidence']:.1f}"
                )
                self.place_sl_tp(symbol, order, price, side, amount, atr)
                return order
            except Exception as e:
                logger.warning("open retry %s/%s failed: %s", attempt + 1, self.cfg["retry_attempts"], e)
                time.sleep(1)
        self.notifier.alert(f"Failed to open position {symbol}", "")
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

    def _place_reduce_only(self, symbol, order_type, side, amount, price=None, params=None):
        base = {"reduceOnly": True}
        if params:
            base.update(params)
        return self.exchange.create_order(symbol, order_type, side, amount, price=price, params=base)

    def _place_stop(self, symbol, side, amount, sl_price):
        try:
            return self.exchange.create_order(
                symbol,
                "stop_market",
                side,
                amount,
                params={"reduceOnly": True, "stopPrice": sl_price},
            )
        except Exception:
            return self.exchange.create_order(
                symbol,
                "limit",
                side,
                amount,
                price=sl_price,
                params={"reduceOnly": True, "stopLossPrice": sl_price},
            )

    def close_all(self, symbol):
        positions = self.exchange.fetch_positions([symbol])
        for pos in positions:
            contracts = float(pos.get("contracts") or 0)
            if contracts == 0:
                continue
            side = "sell" if contracts > 0 else "buy"
            try:
                self.exchange.create_order(symbol, "market", side, abs(contracts), params={"reduceOnly": True})
            except Exception:
                try:
                    side_param = "LONG" if contracts > 0 else "SHORT"
                    self.exchange.create_order(
                        symbol,
                        "market",
                        side,
                        abs(contracts),
                        params={"reduceOnly": True, "positionSide": side_param},
                    )
                except Exception as e:
                    self.notifier.alert(f"Failed to close {symbol}", str(e))
                    continue
            self.notifier.info(f"[CLOSE] {symbol} {side} {abs(contracts)}")

    def cancel_pending(self, symbol=None):
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            for o in orders:
                self.exchange.cancel_order(o["id"], o["symbol"])
            return orders
        except Exception as e:
            logger.warning("cancel pending failed: %s", e)
            return []
