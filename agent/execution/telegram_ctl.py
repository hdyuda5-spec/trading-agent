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

    def _send(self, text, menu=True):
        if not text:
            return
        try:
            payload = {"chat_id": self.chat_id, "text": text}
            if menu:
                payload["reply_markup"] = self._menu_markup()
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
                timeout=10,
            )
        except Exception:
            pass

    def _menu_markup(self):
        return {
            "keyboard": [
                ["📊 /dashboard", "🛡️ /orders"],
                ["📈 /positions", "💰 /balance"],
                ["📉 /metrics", "📋 /trades"],
                ["🧠 /memori", "❤️ /health"],
                ["📡 /trend", "🔎 /screen"],
                ["❓ /help"],
            ],
            "resize_keyboard": True,
        }

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
            "/dashboard": self._dashboard,
            "/all": self._dashboard,
            "/ringkasan": self._dashboard,
            "/positions": self._positions,
            "/orders": self._orders,
            "/balance": self._balance,
            "/metrics": self._metrics,
            "/trades": self._trades,
            "/memori": self._memori,
            "/lessons": self._memori,
            "/trend": self._trend,
            "/screen": self._screen_cmd,
            "/health": self._health,
        }
        handler = handlers.get(cmd)
        return handler() if handler else self._help()

    def _help(self):
        return (
            "Perintah tersedia:\n"
            "/dashboard - SEMUA info dalam 1 pesan\n"
            "/positions - posisi terbuka\n"
            "/orders - SL/TP per posisi\n"
            "/balance - saldo USDT\n"
            "/metrics - win rate & PnL\n"
            "/trades - riwayat trade terakhir\n"
            "/memori - pelajaran dari kesalahan\n"
            "/trend - arah tren tiap simbol\n"
            "/screen - screening koin (trend/volume)\n"
            "/health - uptime & kesehatan bot\n"
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
            side = self.bot._pos_side(p)
            lines.append(
                f"{p.get('symbol')} {side.upper()} contracts={abs(float(p.get('contracts') or 0))} "
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

    def _sl_tp_for(self, symbol):
        sl = []
        tp = []
        try:
            base = symbol.replace("/USDT:USDT", "USDT")
            algos = self.bot.exchange.client.fapiPrivateGetOpenAlgoOrders({"symbol": base}) or []
            sl = [
                str(o.get("triggerPrice"))
                for o in algos
                if (o.get("orderType") or "").startswith("STOP") and float(o.get("quantity") or 0) > 0
            ]
        except Exception:
            pass
        try:
            reg = self.bot.exchange.fetch_open_orders(symbol) or []
            tp = [str(o.get("price")) for o in reg if o.get("reduceOnly")]
        except Exception:
            pass
        return sl, tp

    def _orders(self):
        try:
            positions = self.bot.exchange.fetch_positions()
        except Exception as e:
            return f"Gagal ambil posisi: {e}"
        open_pos = [p for p in positions if abs(float(p.get("contracts") or 0)) > 0]
        if not open_pos:
            return "Tidak ada posisi -> tidak ada SL/TP."
        lines = ["🛡️ SL/TP per posisi:"]
        for p in open_pos:
            sym = p["symbol"]
            sl, tp = self._sl_tp_for(sym)
            side = self.bot._pos_side(p)
            lines.append(f"• {sym} {side.upper()}: SL {sl[0] if sl else '-'} | TP {tp[0] if tp else '-'}")
        return "\n".join(lines)

    def _trades(self):
        try:
            rows = self.bot.store._db.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT 8"
            ).fetchall()
        except Exception as e:
            return f"Gagal baca riwayat: {e}"
        if not rows:
            return "Belum ada trade."
        lines = ["📋 Trade terakhir:"]
        for r in rows:
            ts = time.strftime("%d/%m %H:%M", time.localtime(r[1]))
            icon = "✅" if r[8] >= 0 else "❌"
            lines.append(
                f"{icon} {r[2]} {r[3].upper()} {r[8]:+.2f} USDT ({r[9]:+.1f}%) "
                f"| {r[10]} | {r[4] or '-'} | {ts}"
            )
        return "\n".join(lines)

    def _memori(self):
        lessons = self.bot.store.recent_lessons(10)
        if not lessons:
            return "Belum ada memori (belum ada trade yang ditutup)."
        return "🧠 Memori / pelajaran dari kesalahan:\n" + "\n".join(f"• {l}" for l in lessons)

    def _health(self):
        b = self.bot
        uptime = time.time() - b._start_time
        last_tick = time.time() - b._last_tick
        sc = b.config.get("screener", {})
        ai = b.config["strategies"].get("ai_signal", {})
        return "\n".join([
            f"Proses: {'HALTED' if b.halted else 'running'}",
            f"Uptime: {int(uptime // 60)}m {int(uptime % 60)}s",
            f"Tick terakhir: {int(last_tick)}s lalu",
            f"Auto-screen: {'aktif' if sc.get('enabled') else 'mati'} ({sc.get('auto_interval_minutes', 30)}m)",
            f"AI interval: {ai.get('min_interval_seconds', 60)}s",
            f"Posisi aktif: {len([p for p in b.exchange.fetch_positions() if abs(float(p.get('contracts') or 0)) > 0])}",
        ])

    def _dashboard(self):
        b = self.bot
        lines = ["📊 DASHBOARD"]
        uptime = time.time() - b._start_time
        last_tick = time.time() - b._last_tick
        lines.append(
            f"🤖 Bot: {'HALTED' if b.halted else 'RUNNING'} | up {int(uptime // 60)}m "
            f"| tick {int(last_tick)}s lalu | testnet={b.exchange.testnet}"
        )
        try:
            usdt = b.exchange.fetch_balance().get("USDT", {})
            total = float(usdt.get("total") or 0)
            free = float(usdt.get("free") or 0)
            chg = f" ({(total / b.initial_equity - 1) * 100:+.2f}%)" if b.initial_equity else ""
            lines.append(f"💰 Equity: {total:.2f} USDT | free {free:.2f}{chg}")
        except Exception as e:
            lines.append(f"💰 Equity: gagal ({e})")
        try:
            positions = b.exchange.fetch_positions()
        except Exception:
            positions = []
        open_pos = [p for p in positions if abs(float(p.get("contracts") or 0)) > 0]
        if open_pos:
            lines.append("📈 Posisi:")
            for p in open_pos:
                side = b._pos_side(p)
                sl, tp = self._sl_tp_for(p["symbol"])
                pnl = p.get("unrealizedPnl")
                lines.append(
                    f"• {p['symbol']} {side.upper()} qty={abs(float(p.get('contracts') or 0))} "
                    f"entry={p.get('entryPrice')} pnl={pnl}"
                )
                lines.append(f"   🛡️ SL={sl[0] if sl else '-'} 🎯 TP={tp[0] if tp else '-'}")
        else:
            lines.append("📈 Posisi: tidak ada")
        m = b.store.metrics()
        if m:
            lines.append(
                f"📉 Metrik: {m['trades']} trade | win {m['win_rate']}% | "
                f"PF {m['profit_factor']} | net {m['net_pnl']} USDT"
            )
        lessons = b.store.recent_lessons(3)
        if lessons:
            lines.append("🧠 Memori (3 terakhir):")
            for l in lessons:
                lines.append(f"• {l[:120]}")
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
