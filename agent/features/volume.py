"""Volume feature.

Reports volume pressure from two perspectives:

- ``vol_ratio``: last closed candle volume vs the mean of the prior window
  (same formula the screener used, so its ``vol_x`` output is unchanged).
- ``obv`` trend: On-Balance-Volume momentum over the trailing window.
"""

from typing import Any

import pandas as pd

from agent.features.base import BaseFeature, FeatureResult


class VolumeFeature(BaseFeature):
    name = "volume"
    df_only = True

    def _load_config(self) -> None:
        super()._load_config()
        cfg = self.config.get("features", {}).get(self.name, {}) or {}
        self.ratio_window = int(cfg.get("ratio_window", 20))
        self.obv_window = int(cfg.get("obv_window", 20))
        self.rising_threshold = float(cfg.get("rising_threshold", 1.2))
        self.falling_threshold = float(cfg.get("falling_threshold", 0.8))

    @staticmethod
    def obv_trend(close: Any, volume: Any, window: int = 20) -> str:
        obv: list = []
        running = 0.0
        for j in range(1, len(close)):
            c = float(close.iloc[j])
            pc = float(close.iloc[j - 1])
            v = float(volume.iloc[j])
            if c > pc:
                running += v
            elif c < pc:
                running -= v
            obv.append(running)
        if not obv:
            return "flat"
        s = pd.Series(obv)
        cur = s.iloc[-1]
        avg = s.iloc[-window - 1 : -1].mean() if len(s) > window else s.mean()
        if cur > avg * 1.005:
            return "rising"
        if cur < avg * 0.995:
            return "falling"
        return "flat"

    def compute(self, symbol: str, df: Any) -> FeatureResult:
        volume = df["volume"]
        n = len(df)
        if n < self.ratio_window + 2:
            return FeatureResult.neutral(self.name, metadata={"reason": "insufficient_data"})

        last = float(volume.iloc[-1])
        prev_window = float(volume.iloc[-2 - self.ratio_window : -2].mean()) or 0.0
        vol_ratio = float(volume.iloc[-2]) / prev_window if prev_window else 0.0

        if vol_ratio >= self.rising_threshold:
            signal, confidence = "rising", min(0.95, 0.5 + (vol_ratio - 1.0) * 0.4)
        elif 0 < vol_ratio <= self.falling_threshold:
            signal, confidence = "falling", min(0.95, 0.5 + (1.0 - vol_ratio) * 0.4)
        else:
            signal, confidence = "neutral", 0.5

        metadata = {
            "volume": round(last, 4),
            "vol_ratio": round(vol_ratio, 2),
            "obv_trend": self.obv_trend(df["close"], volume, self.obv_window),
            "ratio_window": self.ratio_window,
        }
        return FeatureResult(self.name, signal, confidence, metadata)
