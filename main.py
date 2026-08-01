import argparse
import json
import os

from agent.bot import TradingBot
from agent.core.utils import setup_logger


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="AI Trading Agent untuk CEX USDT-M Futures")
    parser.add_argument("--config", default="config.json", help="Path ke file config")
    parser.add_argument("--check", action="store_true", help="Cek koneksi exchange lalu keluar")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logger(
        config["logging"].get("level", "INFO"),
        config["logging"].get("file", "logs/agent.log"),
    )

    from agent.core.exchange import ExchangeClient

    ex = ExchangeClient(
        name=config["exchange"]["name"],
        testnet=config["exchange"].get("testnet", True),
        options=config["exchange"].get("options", {}),
    )
    if args.check:
        ok = ex.check_health()
        print(f"Exchange: {config['exchange']['name']} | testnet={ex.testnet} | healthy={ok}")
        try:
            balance = ex.fetch_balance()
            usdt = balance.get("USDT", {})
            print(f"USDT balance: total={usdt.get('total')} free={usdt.get('free')}")
        except Exception as e:
            print(f"Balance fetch failed: {e}")
        return

    TradingBot(config).run()


if __name__ == "__main__":
    main()
