import os
import time

import ccxt
import ccxt.async_support as ccxt_async
from dotenv import load_dotenv

load_dotenv()

EXCHANGE_KEYS = {
    "binance": {
        "apiKey": "BINANCE_API_KEY",
        "secret": "BINANCE_API_SECRET",
    },
    "bybit": {
        "apiKey": "BYBIT_API_KEY",
        "secret": "BYBIT_API_SECRET",
    },
    "okx": {
        "apiKey": "OKX_API_KEY",
        "secret": "OKX_API_SECRET",
        "password": "OKX_API_PASSPHRASE",
    },
}


class ExchangeClient:
    def __init__(self, name="binance", testnet=True, options=None):
        self.name = name
        self.testnet = testnet
        self.options = options or {}
        exchange_class = getattr(ccxt, name)
        self.client = exchange_class(self._build_config())
        if self.testnet:
            self.client.set_sandbox_mode(True)
        self.client.load_markets()

    def _build_config(self):
        key_map = EXCHANGE_KEYS.get(self.name)
        config = {}
        if key_map:
            for param, env_name in key_map.items():
                value = os.getenv(env_name, "")
                if value:
                    config[param] = value
        config["enableRateLimit"] = True
        config["options"] = self.options
        if self.testnet:
            config.update(self._testnet_config())
        return config

    def _testnet_config(self):
        return {}

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=200, since=None):
        if since is not None:
            return self.client.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        return self.client.fetch_ohlcv(symbol, timeframe, limit=limit)

    def fetch_balance(self):
        return self.client.fetch_balance()

    def fetch_positions(self, symbols=None):
        return self.client.fetch_positions(symbols)

    def set_leverage(self, symbol, leverage):
        try:
            return self.client.set_leverage(leverage, symbol)
        except Exception:
            return None

    def set_margin_mode(self, symbol, mode="isolated"):
        try:
            return self.client.set_margin_mode(mode, symbol)
        except Exception:
            return None

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        params = params or {}
        return self.client.create_order(
            symbol,
            order_type,
            side,
            amount,
            price,
            params,
        )

    def cancel_order(self, order_id, symbol):
        return self.client.cancel_order(order_id, symbol)

    def fetch_order(self, order_id, symbol):
        return self.client.fetch_order(order_id, symbol)

    def fetch_open_orders(self, symbol=None):
        return self.client.fetch_open_orders(symbol)

    def fetch_ticker(self, symbol):
        return self.client.fetch_ticker(symbol)

    def fetch_tickers(self):
        return self.client.fetch_tickers()

    def fetch_order_book(self, symbol, limit=5):
        return self.client.fetch_order_book(symbol, limit)

    def fetch_trades(self, symbol, limit=500):
        return self.client.fetch_trades(symbol, limit=limit)

    def fetch_my_trades(self, symbol, limit=10, since=None):
        return self.client.fetch_my_trades(symbol, limit=limit, since=since)

    def fetch_funding_rate(self, symbol):
        """Current funding rate (fraction, e.g. 0.0001) or None if unavailable."""
        try:
            result = self.client.fetch_funding_rate(symbol)
        except Exception:
            return None
        try:
            rate = result.get("fundingRate")
            return float(rate) if rate is not None else None
        except (TypeError, ValueError):
            return None

    # -- conditional (stop) orders -----------------------------------------
    # Binance now rejects STOP_MARKET on /fapi/v1/order (-4120) and requires
    # the Algo Order API (POST /fapi/v1/algoOrder, algotype=CONDITIONAL).
    # ccxt 4.x still routes create_order(stop_market) to the old endpoint, so
    # we call the raw fapi endpoint directly for binance futures.

    def _market_id(self, symbol):
        try:
            m = self.client.markets.get(symbol) or {}
            return m.get("id") or symbol.replace("/USDT:USDT", "USDT")
        except Exception:
            return symbol.replace("/USDT:USDT", "USDT")

    def create_stop_order(self, symbol, side, stop_price, amount=None, position_side=None):
        """Place a market-triggered stop order that closes the whole position.

        Uses the Binance Futures Algo Order endpoint (``closePosition``), so
        small positions are not rejected on minimum notional. Falls back to the
        classic ``create_order(stop_market)`` path on other exchanges.
        """
        if self.name != "binance":
            return self.client.create_order(
                symbol,
                "stop_market",
                side,
                amount,
                params={"reduceOnly": True, "stopPrice": stop_price},
            )
        try:
            return self.client.fapiPrivatePostAlgoOrder(
                {
                    "symbol": self._market_id(symbol),
                    "algotype": "CONDITIONAL",
                    "type": "STOP_MARKET",
                    "side": side.upper(),
                    "triggerPrice": self.client.price_to_precision(symbol, stop_price),
                    "positionSide": position_side or "BOTH",
                    "workingType": "CONTRACT_PRICE",
                    "closePosition": "true",
                }
            )
        except Exception:
            if amount is None:
                raise
            return self.client.create_order(symbol, "stop_market", side, amount, params={"reduceOnly": True, "stopPrice": stop_price})

    def fetch_open_stop_orders(self, symbol=None):
        """List open algo (conditional) stop orders."""
        if self.name != "binance":
            return []
        try:
            params = {"symbol": self._market_id(symbol)} if symbol else {}
            result = self.client.fapiPrivateGetOpenAlgoOrders(params)
            if isinstance(result, dict):
                return result.get("orders") or []
            return result or []
        except Exception:
            return []

    def cancel_stop_order(self, algo_id, symbol):
        if self.name != "binance":
            return None
        try:
            return self.client.fapiPrivateDeleteAlgoOrder(
                {"symbol": self._market_id(symbol), "algoId": algo_id}
            )
        except Exception:
            return None

    def check_health(self):
        try:
            self.client.fetch_time()
            return True
        except Exception:
            return False

    @staticmethod
    def tick():
        time.sleep(0.5)
