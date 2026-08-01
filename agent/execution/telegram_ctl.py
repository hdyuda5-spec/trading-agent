import threading
import time

import requests

from agent.core.screener import Screener


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

    def _reply_async(self, fn):
        def run():
            try:
                self._send(fn())
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _handle(self, text):
        cmd = text.split()[0].split("@")[0].lower()
        handlers = {
            "/start": self._instruksi,
            "/help": self._help,
            "/instruksi": self._instruksi,
            "/status": self._status,
            "/positions": self._positions,
            "/balance": self._balance,
            "/metrics": self._metrics,
            "/trend": self._trend,
            "/screen": self._screen_cmd,
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
            "/trend - arah tren tiap simbol\n"
            "/screen - screening koin (trend/volume)\n"
            "/instruksi - panduan lengkap\n"
            "/help - bantuan ini"
        )

    def _instruksi(self):
        return (
            "🤖 Trading Agent — Panduan\n\n"
            "1) Setup\n"
            "- git clone https://github.com/hdyuda5-spec/trading-agent\n"
            "- pip install -r requirements.txt\n"
            "- cp .env.example .env lalu isi API key\n\n"
            "2) Config (config.json)\n"
            "- pastikan \"testnet\": true\n"
            "- aktifkan strategi: momentum / ai_signal / grid\n"
            "- atur risiko: leverage, max_position_pct, dll.\n\n"
            "3) Jalankan\n"
            "- python main.py\n\n"
            "4) Perintah bot\n"
            "- /status /positions /balance /metrics /trend /screen\n\n"
            "5) Otomatis\n"
            "- notifikasi order & error\n"
            "- laporan harian jam 08:00\n"
            "- screening koin di laporan harian\n\n"
            "⚠️ Selalu mulai dengan testnet. Bot ini bukan saran keuangan."
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
        text = self._positions_text()
        return text or "Tidak ada posisi terbuka."

    def _positions_text(self):
        try:
            positions = self.bot.exchange.fetch_positions()
        except Exception as e:
            return f"Gagal ambil posisi: {e}"
        open_positions = [p for p in positions if abs(float(p.get("contracts") or 0)) > 0]
        if not open_positions:
            return None
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

    def _screen_cmd(self):
        self._reply_async(self._screen)
        return "Menyaring pasar... hasil dikirim sebentar."

    def _screen(self):
        try:
            screener = Screener(self.bot.exchange, self.bot.config)
            results = screener.candidates()
            return screener.format(results, title=f"Screening {screener.timeframe}")
        except Exception as e:
            return f"Screening gagal: {e}"

    def send_daily_report(self):
        if not self.enabled:
            return
        b = self.bot
        day = time.strftime("%Y-%m-%d")
        lines = [f"📊 Laporan Harian {day}"]
        equity = b.initial_equity
        now_eq = None
        try:
            now_eq = b._equity()
        except Exception:
            pass
        if now_eq is not None and equity:
            chg = (now_eq / equity - 1) * 100
            lines.append(f"Equity: {now_eq:.2f} USDT (start {equity:.2f}, {chg:+.2f}%)")
        positions = self._positions_text()
        lines.append("Posisi:\n" + (positions or "tidak ada"))
        metrics = b.store.metrics()
        if metrics:
            lines.append(
                f"Metrik: trades={metrics['trades']} win={metrics['win_rate']}% "
                f"pf={metrics['profit_factor']} net={metrics['net_pnl']}"
            )
        self._send("\n".join(lines))
        self._reply_async(self._screen)
