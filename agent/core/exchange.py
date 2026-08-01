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

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=200):
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

    def check_health(self):
        try:
            self.client.fetch_time()
            return True
        except Exception:
            return False

    @staticmethod
    def tick():
        time.sleep(0.5)
