import pandas as pd


def _trade_side(t):
    side = t.get("side")
    if side:
        return side
    is_maker = (t.get("info") or {}).get("m")
    return "sell" if is_maker in (True, "true", 1, "1") else "buy"


class SmartMoneyAnalyzer:
    def __init__(self, exchange, config):
        cfg = config.get("smart_money", {})
        self.enabled = cfg.get("enabled", True)
        self.trades_limit = int(cfg.get("cvd_trades_limit", 500))
        self.book_depth = int(cfg.get("orderbook_depth", 10))
        self.exchange = exchange

    def analyze(self, symbol, df):
        if not self.enabled:
            return None
        result = {}
        try:
            result["book"] = self._orderbook(symbol)
        except Exception:
            result["book"] = None
        try:
            result["cvd"] = self._cvd(symbol)
        except Exception:
            result["cvd"] = None
        try:
            result["obv"] = self._obv(df)
        except Exception:
            result["obv"] = None
        result["score"] = self._score(result)
        return result

    def _orderbook(self, symbol):
        book = self.exchange.fetch_order_book(symbol, self.book_depth)
        bid = sum(float(p[1]) for p in (book.get("bids") or []))
        ask = sum(float(p[1]) for p in (book.get("asks") or []))
        if bid + ask <= 0:
            return {"imbalance": 0.0}
        return {"imbalance": round((bid - ask) / (bid + ask), 4)}

    def _cvd(self, symbol):
        trades = self.exchange.fetch_trades(symbol, limit=self.trades_limit)
        buy = 0.0
        sell = 0.0
        for t in trades:
            amount = float(t.get("amount") or 0)
            if _trade_side(t) == "buy":
                buy += amount
            else:
                sell += amount
        total = buy + sell
        if total <= 0:
            return {"net": 0.0, "buy_pct": 50.0}
        return {
            "net": round(buy - sell, 2),
            "buy_pct": round(buy / total * 100, 1),
        }

    def _obv(self, df):
        close = df["close"]
        volume = df["volume"]
        obv = [0.0]
        for j in range(1, len(close)):
            if close.iloc[j] > close.iloc[j - 1]:
                obv.append(obv[-1] + float(volume.iloc[j]))
            elif close.iloc[j] < close.iloc[j - 1]:
                obv.append(obv[-1] - float(volume.iloc[j]))
            else:
                obv.append(obv[-1])
        s = pd.Series(obv)
        cur = s.iloc[-1]
        avg = s.iloc[-21:-1].mean() if len(s) > 21 else s.mean()
        if cur > avg * 1.005:
            return {"trend": "rising"}
        if cur < avg * 0.995:
            return {"trend": "falling"}
        return {"trend": "flat"}

    def _score(self, result):
        score = 0
        details = []
        book = result.get("book")
        if book:
            imb = book["imbalance"]
            if imb >= 0.15:
                score += 1
                details.append("book+")
            elif imb <= -0.15:
                score -= 1
                details.append("book-")
        cvd = result.get("cvd")
        if cvd:
            if cvd["buy_pct"] >= 55:
                score += 1
                details.append("cvd+")
            elif cvd["buy_pct"] <= 45:
                score -= 1
                details.append("cvd-")
        obv = result.get("obv")
        if obv:
            if obv["trend"] == "rising":
                score += 1
                details.append("obv+")
            elif obv["trend"] == "falling":
                score -= 1
                details.append("obv-")
        direction = "LONG" if score > 0 else ("SHORT" if score < 0 else "NEUTRAL")
        return {"direction": direction, "score": score, "details": details}
