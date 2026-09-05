"""Shared pre-trade filters used by the signal and screener services."""

import logging
import time

from agent.core.whale import should_execute_trade

logger = logging.getLogger("trading-agent")


class TradeFilters:
    """Stateless pre-trade gates. All inputs injected; no exchange access here."""

    def __init__(self, config):
        self.config = config

    def trading_hours_ok(self) -> bool:
        """Block auto-trade during configured no-trade hours (WIB = UTC+7)."""
        sc = self.config.get("screener", {}) or {}
        blocked = set(int(h) for h in (sc.get("no_trade_hours_wib", []) or []))
        if not blocked:
            return True
        utc_hour = int(time.time() // 3600) % 24
        wib_hour = (utc_hour + 7) % 24
        return wib_hour not in blocked

    def losing_streak(self, symbol, side, store):
        limit = int(self.config["risk"].get("skip_after_consecutive_losses", 0))
        if limit <= 0:
            return False
        recent = store.recent_trades(symbol, side, limit)
        return len(recent) >= limit and all(r["pnl"] < 0 for r in recent)

    def whale_filter_ok(self, symbol, side, whale):
        wcfg = self.config.get("whale", {})
        if not wcfg.get("filter_trades", True):
            return True, ""
        event = whale.data(symbol)
        if not event:
            return True, "tidak ada data whale"
        whale_data = {
            "net_flow_usdt": event["net_usdt"],
            "transaction_count": event["n"],
        }
        signal = "BUY" if side == "LONG" else "SELL"
        min_txns = int(wcfg.get("min_whale_txns", 3))
        min_net = float(wcfg.get("min_alert_net_usdt", 0))
        return should_execute_trade(signal, whale_data, min_txns, min_net)

    def market_regime_allows(self, side, whale, symbols_provider):
        wcfg = self.config.get("whale", {})
        reg = wcfg.get("market_regime", {})
        if not reg.get("enabled", False):
            return True
        threshold = float(reg.get("net_sell_threshold_usdt", 0))
        min_symbols = int(reg.get("min_symbols", 5))
        total, with_data = whale.market_net_flow(symbols_provider)
        if with_data < min_symbols:
            return True
        if total <= -threshold and side == "LONG":
            logger.info("[REGIME] skip LONG: aggregate whale net sell %+.0f USDT", total)
            return False
        if total >= threshold and side == "SHORT":
            logger.info("[REGIME] skip SHORT: aggregate whale net buy %+.0f USDT", total)
            return False
        return True

    def rsi_confirmation_ok(self, side, rsi, cfg=None):
        cfg = cfg or self.config.get("screener", {})
        if not cfg.get("rsi_confirmation", False):
            return True
        if side == "LONG":
            long_r = cfg.get("rsi_long_range", [40, 75])
            return long_r[0] <= rsi <= long_r[1]
        short_r = cfg.get("rsi_short_range", [25, 60])
        return short_r[0] <= rsi <= short_r[1]

    def not_extreme_move(self, chg, cfg=None):
        cfg = cfg or self.config.get("screener", {})
        max_ext = float(cfg.get("max_extended_pct", 0))
        if max_ext <= 0:
            return True
        return abs(float(chg)) <= max_ext

    @staticmethod
    def conflict_reason(r, side):
        pat = r.get("pattern") or {}
        if pat.get("direction") == "bullish" and side == "SHORT":
            return f"pattern {pat['name']} kontra"
        if pat.get("direction") == "bearish" and side == "LONG":
            return f"pattern {pat['name']} kontra"
        sm = r.get("smart_money") or {}
        if sm.get("direction") == "LONG" and side == "SHORT":
            return "smart money LONG vs SHORT"
        if sm.get("direction") == "SHORT" and side == "LONG":
            return "smart money SHORT vs LONG"
        return None
