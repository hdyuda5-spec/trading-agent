"""Market screener.

The screener now consumes the Feature Engine instead of computing indicators
inline. Its public surface (``candidates`` / ``format`` and the per-symbol
result dict) is unchanged so callers in ``bot.py`` and ``telegram_ctl.py``
keep working identically.
"""

from agent.features import build_feature_engine
from agent.core.utils import ohlcv_to_dataframe

_SIGNAL_TO_SIDE = {"bullish": "LONG", "bearish": "SHORT", "neutral": "NEUTRAL"}


class Screener:
    def __init__(self, exchange, config, feature_engine=None):
        self.exchange = exchange
        cfg = config.get("screener", {})
        self.timeframe = cfg.get("timeframe", "1h")
        self.max_coins = int(cfg.get("max_coins", 10))
        self.min_volume_usdt = float(cfg.get("min_volume_usdt", 0))
        self.exclude_symbols = set(cfg.get("exclude_symbols", []))
        self.engine = feature_engine or build_feature_engine(exchange, config)

    def candidates(self, limit=None):
        limit = limit or self.max_coins
        rows = []
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            return []
        for symbol, t in tickers.items():
            if not symbol.endswith("/USDT:USDT"):
                continue
            if symbol in self.exclude_symbols:
                continue
            qv = float(t.get("quoteVolume") or 0)
            chg = float(t.get("percentage") or 0)
            if qv >= self.min_volume_usdt:
                rows.append((symbol, qv, chg))
        rows.sort(key=lambda r: -r[1])
        results = []
        for symbol, _, _ in rows[:limit]:
            info = self._analyze(symbol)
            if info:
                results.append(info)
        return results

    def _analyze(self, symbol):
        try:
            df = ohlcv_to_dataframe(self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=100))
            if len(df) < 52:
                return None
            fs = self.engine.compute(symbol, df)
            trend = fs.trend
            volume = fs.volume
            volatility = fs.volatility
            structure = fs.structure
            sentiment = fs.sentiment
            close = df["close"]
            ema9 = trend.metadata.get("ema9")
            ema21 = trend.metadata.get("ema21")
            ema50 = trend.metadata.get("ema50")
            if ema9 is not None and ema21 is not None and ema50 is not None:
                if ema9 > ema21 > ema50:
                    side = "LONG"
                elif ema9 < ema21 < ema50:
                    side = "SHORT"
                else:
                    side = "NEUTRAL"
            else:
                side = "NEUTRAL"
            chg = float(close.iloc[-1] / close.iloc[-25] - 1) * 100
            return {
                "symbol": symbol,
                "trend": side,
                "rsi": round(trend.metadata.get("rsi") or 0.0, 1),
                "vol": round(volume.metadata.get("vol_ratio") or 0.0, 1),
                "chg": round(chg, 2),
                "price": float(close.iloc[-1]),
                "atr": round(volatility.metadata.get("atr") or 0.0, 8),
                "pattern": structure.metadata.get("pattern"),
                "smart_money": dict(sentiment.metadata) or None,
            }
        except Exception:
            return None

    def format(self, results, title="Screening"):
        if not results:
            return f"{title}: tidak ada kandidat"
        groups = {"LONG": [], "SHORT": [], "NEUTRAL": []}
        for r in results:
            groups.setdefault(str(r.get("trend") or "NEUTRAL"), []).append(r)
        icons = {"LONG": "🟢", "SHORT": "🔴", "NEUTRAL": "⚪"}
        lines = [title]
        for trend in ("LONG", "SHORT", "NEUTRAL"):
            grp = groups.get(trend, [])
            if not grp:
                continue
            lines.append(f"{icons[trend]} {trend} ({len(grp)})")
            for i, r in enumerate(grp, 1):
                pat = r.get("pattern") or {}
                pat_txt = f" · {pat['name']}" if pat.get("name") else ""
                chg = r.get("chg")
                chg_txt = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "-"
                sym = r["symbol"].replace("/USDT:USDT", "/USDT")
                lines.append(f"  {i}. {sym} · RSI {r.get('rsi')} · {chg_txt}{pat_txt}")
        return "\n".join(lines)
