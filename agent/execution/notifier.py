import logging
import os
import time

import requests

logger = logging.getLogger("trading-agent")

_ID_MONTHS = [
    "", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]

_PATTERN_LABELS = {
    "momentum": "EMA Crossover + RSI",
    "ai_signal": "AI Signal (LLM)",
    "grid": "Grid",
    "screener": "Screener EMA + RSI",
}

_CLOSE_REASON_LABELS = {
    "sl_tp": "SL / TP",
    "trailing": "Trailing Stop",
    "reversal": "Sinyal Reversal",
    "daily_loss": "Daily Loss Limit",
}


def fmt_wib(ts=None):
    if ts is None:
        ts = time.time()
    t = time.gmtime(ts + 7 * 3600)
    return f"{t.tm_mday:02d} {_ID_MONTHS[t.tm_mon]} {t.tm_year}, {t.tm_hour:02d}:{t.tm_min:02d} WIB"


def format_signal_card(symbol, side, entry, sl, tp1, tp2, pattern="", ts=None):
    side_label = "BUY" if side == "LONG" else "SELL"
    icon = "🎯" if side == "LONG" else "🔴"
    sym = symbol.replace("/USDT:USDT", "/USDT")
    pct = lambda p: f"{p:+.1f}%"
    diff = lambda v: (v / entry - 1) * 100 if entry else 0.0
    return "\n".join([
        f"{icon} SINYAL {side_label} — {sym}",
        f"📊 Pattern: {pattern or '-'}",
        f"💰 Entry: {entry:,.2f}",
        f"🛡️ SL: {sl:,.2f} ({pct(diff(sl))})",
        f"🎯 TP1: {tp1:,.2f} ({pct(diff(tp1))})",
        f"🎯 TP2: {tp2:,.2f} ({pct(diff(tp2))})",
        f"⏰ {fmt_wib(ts)}",
    ])


def format_close_card(symbol, side, entry, exit_px, qty, pnl, pnl_pct, reason="", strategy="", ts=None):
    sym = symbol.replace("/USDT:USDT", "/USDT")
    side_label = "LONG" if side == "long" else "SHORT"
    icon = "✅" if pnl >= 0 else "❌"
    reason_label = _CLOSE_REASON_LABELS.get(reason, reason or "-")
    return "\n".join([
        f"{icon} POSISI DITUTUP — {sym}",
        f"📊 Strategi: {strategy or '-'}",
        f"📈 Side: {side_label}",
        f"💰 Entry: {entry:,.2f}",
        f"💱 Exit: {exit_px:,.2f}",
        f"🧮 Qty: {qty:.4f}",
        f"💵 PnL: {pnl:+.2f} USDT ({pnl_pct:+.1f}%)",
        f"📌 Alasan: {reason_label}",
        f"⏰ {fmt_wib(ts)}",
    ])


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

    def send(self, text):
        logger.info(text.replace("\n", " | "))
        if self.enabled and self.bot_token and self.chat_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text},
                    timeout=10,
                )
            except Exception:
                pass

    def send_signal(self, symbol, side, entry, sl, tp1, tp2, strategy="", ts=None):
        pattern = _PATTERN_LABELS.get(strategy, strategy or "-")
        text = format_signal_card(symbol, side, entry, sl, tp1, tp2, pattern, ts)
        logger.info(text.replace("\n", " | "))
        if self.enabled and self.bot_token and self.chat_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text},
                    timeout=10,
                )
            except Exception:
                pass

    def send_close(self, symbol, side, entry, exit_px, qty, pnl, pnl_pct, reason="", strategy="", ts=None):
        text = format_close_card(symbol, side, entry, exit_px, qty, pnl, pnl_pct, reason, strategy, ts)
        logger.info(text.replace("\n", " | "))
        if self.enabled and self.bot_token and self.chat_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text},
                    timeout=10,
                )
            except Exception:
                pass
