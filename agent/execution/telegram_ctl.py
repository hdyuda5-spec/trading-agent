import threading
import time

import requests


class TelegramController:
    def __init__(self, bot):
        self.bot = bot
        notifier = bot.notifier
        self.enabled = notifier.enabled and bool(notifier.bot_token) and bool(notifier.chat_id)
        self.token = notifier.bot_token
        self.chat_id = str(notifier.chat_id)
        self._offset = None
        self._thread = None

    def start(self):
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                updates = self._get_updates(self._offset)
                for update in updates:
                    self._offset = int(update["update_id"]) + 1
                    msg = update.get("message")
                    if not msg or str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue
                    text = msg.get("text", "").strip()
                    self._send(self._handle(text))
            except Exception:
                pass
            time.sleep(2)

    def _get_updates(self, offset):
        resp = requests.get(
            f"https://api.telegram.org/bot{self.token}/getUpdates",
            params={"timeout": 25, "offset": offset} if offset else {"timeout": 25},
            timeout=35,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    def _send(self, text):
        if not text:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
        except Exception:
            pass

    def _handle(self, text):
        cmd = text.split()[0].split("@")[0].lower()
        handlers = {
            "/start": self._help,
            "/help": self._help,
            "/status": self._status,
            "/positions": self._positions,
            "/balance": self._balance,
            "/metrics": self._metrics,
            "/trend": self._trend,
        }
        handler = handlers.get(cmd)
        return handler() if handler else self._help()

    def _help(self):
        return (
            "Perintah tersedia:\n"
            "/status - ringkasan bot\n"
            "/positions - posisi terbuka\n"
            "/balance - saldo USDT\n"
            "/metrics - win rate & PnL\n"
            "/trend - arah tren tiap simbol"
        )

    def _status(self):
        b = self.bot
        equity = b._equity() if b.initial_equity is not None else None
        strategies = [s.name for s in b.strategies]
        lines = [
            f"Bot: {'HALTED (daily loss)' if b.halted else 'running'}",
            f"Exchange: {b.config['exchange']['name']} (testnet={b.exchange.testnet})",
            f"Symbols: {', '.join(b.config['symbols'])}",
            f"Strategies: {', '.join(strategies)}",
            f"Equity: {equity:.2f} USDT" if equity else "Equity: (perlu API key)",
            f"Initial: {b.initial_equity:.2f}" if b.initial_equity else "Initial: (perlu API key)",
        ]
        return "\n".join(lines)

    def _positions(self):
        try:
            positions = self.bot.exchange.fetch_positions()
        except Exception as e:
            return f"Gagal ambil posisi: {e}"
        open_positions = [p for p in positions if abs(float(p.get("contracts") or 0)) > 0]
        if not open_positions:
            return "Tidak ada posisi terbuka."
        lines = []
        for p in open_positions:
            side = "LONG" if float(p.get("contracts") or 0) > 0 else "SHORT"
            lines.append(
                f"{p.get('symbol')} {side} contracts={abs(float(p.get('contracts') or 0))} "
                f"entry={p.get('entryPrice')} pnl={p.get('unrealizedPnl')}"
            )
        return "\n".join(lines)

    def _balance(self):
        try:
            balance = self.bot.exchange.fetch_balance()
        except Exception as e:
            return f"Gagal ambil saldo: {e}"
        usdt = balance.get("USDT", {})
        return f"USDT total={usdt.get('total')} free={usdt.get('free')}"

    def _metrics(self):
        metrics = self.bot.store.metrics()
        if not metrics:
            return "Belum ada trade tercatat."
        return (
            f"Trades: {metrics['trades']}\n"
            f"Win rate: {metrics['win_rate']}%\n"
            f"Profit factor: {metrics['profit_factor']}\n"
            f"Net PnL: {metrics['net_pnl']}\n"
            f"Avg win: {metrics['avg_win']} / Avg loss: {metrics['avg_loss']}"
        )

    def _trend(self):
        lines = []
        for symbol in self.bot.config["symbols"]:
            direction = self.bot.trend.direction(symbol)
            lines.append(f"{symbol}: {direction or 'n/a'}")
        return "\n".join(lines)
