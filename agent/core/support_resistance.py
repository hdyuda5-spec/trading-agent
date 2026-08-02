class SupportResistance:
    def __init__(self, window=3, cluster_pct=0.5):
        self.window = int(window)
        self.cluster_pct = float(cluster_pct)

    def _swing_points(self, df):
        highs = []
        lows = []
        n = len(df)
        w = self.window
        for j in range(w, n - w):
            h = float(df["high"].iloc[j])
            l = float(df["low"].iloc[j])
            if all(h > float(df["high"].iloc[k]) for k in range(j - w, j + w + 1) if k != j):
                highs.append((j, h))
            if all(l < float(df["low"].iloc[k]) for k in range(j - w, j + w + 1) if k != j):
                lows.append((j, l))
        return highs, lows

    def _cluster(self, levels):
        if not levels:
            return []
        levels = sorted(levels)
        clusters = []
        for v in levels:
            if clusters and abs(v - clusters[-1][-1]) / clusters[-1][-1] * 100 <= self.cluster_pct:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [sum(c) / len(c) for c in clusters]

    def levels(self, df):
        highs, lows = self._swing_points(df)
        return {
            "support": self._cluster([v for _, v in lows]),
            "resistance": self._cluster([v for _, v in highs]),
        }

    def filter(self, side, price, levels, zone_pct):
        sup = sorted([s for s in levels.get("support", []) if s < price])
        res = sorted([r for r in levels.get("resistance", []) if r > price])
        if side == "LONG":
            if res and (res[0] - price) / price * 100 <= zone_pct:
                return False, f"harga {price:.4g} dekat resistance {res[0]:.4g}"
            if sup and (price - sup[-1]) / price * 100 <= zone_pct:
                return True, f"LONG dekat support {sup[-1]:.4g}"
            return True, "S/R netral"
        if side == "SHORT":
            if sup and (price - sup[-1]) / price * 100 <= zone_pct:
                return False, f"harga {price:.4g} dekat support {sup[-1]:.4g}"
            if res and (res[0] - price) / price * 100 <= zone_pct:
                return True, f"SHORT dekat resistance {res[0]:.4g}"
            return True, "S/R netral"
        return True, "no filter applied"
