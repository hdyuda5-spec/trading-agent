import logging
import os

import requests

logger = logging.getLogger("trading-agent")


class Notifier:
    def __init__(self, config):
        self.enabled = config.get("enabled", False)
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", config.get("bot_token", ""))
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", config.get("chat_id", ""))

    def alert(self, message, *details):
        text = message if not details else f"{message}: {details[0]}"
        logger.warning(text)
        if self.enabled and self.bot_token and self.chat_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text},
                    timeout=10,
                )
            except Exception:
                pass

    def info(self, message):
        logger.info(message)
        self.alert(message)
