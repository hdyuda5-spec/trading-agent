from agent.core.patterns import detect_patterns
from agent.core.smart_money import SmartMoneyAnalyzer
from agent.core.utils import compute_atr, compute_ema, compute_rsi, ohlcv_to_dataframe


class Screener:
    def __init__(self, exchange, config):
        self.exchange = exchange
        cfg = config.get("screener", {})
        self.timeframe = cfg.get("timeframe", "1h")
        self.max_coins = int(cfg.get("max_coins", 10))
        self.min_volume_usdt = float(cfg.get("min_volume_usdt", 0))
        self.smart_money = SmartMoneyAnalyzer(exchange, config)

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
            close = df["close"]
            ema9 = float(compute_ema(close, 9).iloc[-1])
            ema21 = float(compute_ema(close, 21).iloc[-1])
            ema50 = float(compute_ema(close, 50).iloc[-1])
            rsi = float(compute_rsi(close, 14).iloc[-1])
            vol_avg = float(df["volume"].iloc[-22:-2].mean()) or 0
            vol_ratio = float(df["volume"].iloc[-2]) / vol_avg if vol_avg else 0
            if ema9 > ema21 > ema50:
                trend = "LONG"
            elif ema9 < ema21 < ema50:
                trend = "SHORT"
            else:
                trend = "NEUTRAL"
            chg = float(close.iloc[-1] / close.iloc[-25] - 1) * 100
            return {
                "symbol": symbol,
                "trend": trend,
                "rsi": round(rsi, 1),
                "vol": round(vol_ratio, 1),
                "chg": round(chg, 2),
                "price": float(close.iloc[-1]),
                "atr": float(compute_atr(df, 14).iloc[-1]) if len(df) > 15 else 0.0,
                "pattern": detect_patterns(df),
                "smart_money": self.smart_money.analyze(symbol, df),
            }
        except Exception:
            return None

    def format(self, results, title="Screening"):
        lines = [title]
        for i, r in enumerate(results, 1):
            pat = r.get("pattern") or {}
            pat_txt = f" {pat['name']}" if pat.get("name") else ""
            sm = r.get("smart_money") or {}
            sm_dir = sm.get("direction") if sm else None
            sm_txt = f" sm={sm_dir}" if sm_dir and sm_dir != "NEUTRAL" else ""
            lines.append(
                f"{i}. {r['symbol']} {r['trend']} rsi={r['rsi']} vol_x{r['vol']} 25c={r['chg']}%{pat_txt}{sm_txt}"
            )
        return "\n".join(lines) if len(lines) > 1 else f"{title}: tidak ada kandidat"
