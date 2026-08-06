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


def fmt_usdt(v):
    """Ringkas angka USDT: 2.98M, 150k, 850 dsb."""
    if v is None:
        return "0"
    if abs(v) >= 1e9:
        return f"{v / 1e9:,.2f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:,.2f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:,.1f}k"
    return f"{v:,.0f}"


def _sym(symbol):
    return symbol.replace("/USDT:USDT", "/USDT")


def _pct_of(value, entry):
    return f"{(value / entry - 1) * 100:+.1f}%" if entry else "-"


def format_signal_card(symbol, side, entry, sl, tp1, tp2, pattern="", ts=None):
    side_label = "BUY" if side == "LONG" else "SELL"
    icon = "🟢" if side == "LONG" else "🔴"
    sym = _sym(symbol)
    lines = [
        f"{icon} SINYAL {side_label} — {sym}",
        "──────────────",
        f"📊 Pattern : {pattern or '-'}",
        f"💰 Entry   : {entry:,.2f}",
        f"🛡️ SL      : {sl:,.2f} ({_pct_of(sl, entry)})",
        f"🎯 TP1     : {tp1:,.2f} ({_pct_of(tp1, entry)})",
        f"🎯 TP2     : {tp2:,.2f} ({_pct_of(tp2, entry)})",
        f"⏰ {fmt_wib(ts)}",
    ]
    return "\n".join(lines)


def format_close_card(symbol, side, entry, exit_px, qty, pnl, pnl_pct, reason="", strategy="", ts=None):
    sym = _sym(symbol)
    side_label = "LONG" if side == "long" else "SHORT"
    icon = "✅" if pnl >= 0 else "❌"
    reason_label = _CLOSE_REASON_LABELS.get(reason, reason or "-")
    pct = f"{pnl_pct:+.1f}%"
    return "\n".join([
        f"{icon} POSISI DITUTUP — {sym} ({side_label})",
        "──────────────",
        f"📊 Strategi: {strategy or '-'}",
        f"💰 Entry   : {entry:,.2f}",
        f"💱 Exit    : {exit_px:,.2f}",
        f"🧮 Qty     : {qty:.6g}",
        f"💵 PnL     : {pnl:+.2f} USDT ({pct})",
        f"📌 Alasan  : {reason_label}",
        f"⏰ {fmt_wib(ts)}",
    ])


def format_open_card(symbol, side, entry, qty, sl=None, tp=None, strategy="", leverage=None, equity=None, ts=None):
    side_label = "LONG" if str(side).upper() in ("LONG", "BUY") else "SHORT"
    icon = "🟢" if side_label == "LONG" else "🔴"
    lines = [
        f"{icon} POSISI TERBUKA — {_sym(symbol)} ({side_label})",
        "──────────────",
        f"📊 Strategi: {strategy or '-'}",
        f"💰 Entry   : {entry:,.2f}",
        f"🧮 Qty     : {qty:.6g}",
    ]
    if sl:
        lines.append(f"🛡️ SL      : {sl:,.2f} ({_pct_of(sl, entry)})")
    if tp:
        lines.append(f"🎯 TP      : {tp:,.2f} ({_pct_of(tp, entry)})")
    if leverage:
        lines.append(f"⚙️ Leverage: {leverage}x · Notional: {entry * qty:,.2f} USDT")
    if equity:
        lines.append(f"💼 Equity  : {equity:,.2f} USDT")
    lines.append(f"⏰ {fmt_wib(ts)}")
    return "\n".join(lines)


class Notifier:
    def __init__(self, config):
        self.enabled = config.get("enabled", False)
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", config.get("bot_token", ""))
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", config.get("chat_id", ""))

    def _tg_send(self, text):
        if not (self.enabled and self.bot_token and self.chat_id):
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
        except Exception:
            pass

    def alert(self, message, *details):
        text = message if not details else f"{message}: {details[0]}"
        logger.warning(text)
        self._tg_send(text)

    def info(self, message):
        logger.info(message)

    def send(self, text):
        logger.info(text.replace("\n", " | "))
        self._tg_send(text)

    def send_signal(self, symbol, side, entry, sl, tp1, tp2, strategy="", ts=None):
        pattern = _PATTERN_LABELS.get(strategy, strategy or "-")
        text = format_signal_card(symbol, side, entry, sl, tp1, tp2, pattern, ts)
        logger.info(text.replace("\n", " | "))
        self._tg_send(text)

    def send_open(self, symbol, side, entry, qty, sl=None, tp=None, strategy="", leverage=None, equity=None, ts=None):
        text = format_open_card(symbol, side, entry, qty, sl, tp, strategy, leverage, equity, ts)
        logger.info(text.replace("\n", " | "))
        self._tg_send(text)

    def send_close(self, symbol, side, entry, exit_px, qty, pnl, pnl_pct, reason="", strategy="", ts=None):
        text = format_close_card(symbol, side, entry, exit_px, qty, pnl, pnl_pct, reason, strategy, ts)
        logger.info(text.replace("\n", " | "))
        self._tg_send(text)
