import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.core.exchange import ExchangeClient
from agent.core.utils import compute_atr, compute_ema, compute_rsi, ohlcv_to_dataframe


class Backtester:
    def __init__(self, config, symbol, timeframe="15m", strategy="momentum", initial_equity=10000.0, fee=0.0002):
        self.cfg = config
        self.symbol = symbol
        self.timeframe = timeframe
        self.strategy = strategy
        self.initial_equity = float(initial_equity)
        self.fee = float(fee)

    def load_data(self, days=90, testnet=True):
        ex = ExchangeClient(name=self.cfg["exchange"]["name"], testnet=testnet,
                            options=self.cfg["exchange"].get("options", {}))
        tf_sec = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
        limit = int((days * 86400) / tf_sec.get(self.timeframe, 900)) + 10
        df = ohlcv_to_dataframe(ex.fetch_ohlcv(self.symbol, self.timeframe, limit=limit))
        ex.client.close()
        return df

    def run(self, df):
        strat = self.cfg["strategies"].get(self.strategy, {})
        if self.strategy == "momentum":
            return self._run_momentum(df, strat)
        raise NotImplementedError(f"strategy {self.strategy} not supported yet")

    def _run_momentum(self, df, strat):
        ema_fast = strat.get("ema_fast", 9)
        ema_slow = strat.get("ema_slow", 21)
        rsi_period = strat.get("rsi_period", 14)
        rsi_oversold = strat.get("rsi_oversold", 30)
        rsi_overbought = strat.get("rsi_overbought", 70)
        require_rsi = strat.get("require_rsi_filter", True)
        cooldown = strat.get("cooldown_seconds", 900) / (CANDLE_TF[self.timeframe] or 900)
        risk = self.cfg["risk"]
        use_atr = risk.get("use_atr_stops", False)
        atr_period = risk.get("atr_period", 14)
        atr_stop = risk.get("atr_stop_mult", 1.5)
        atr_tp = risk.get("atr_tp_mult", 2.5)
        tp_pct = risk.get("take_profit_pct", 2.0) / 100.0
        sl_pct = risk.get("stop_loss_pct", 1.0) / 100.0
        trail_pct = risk.get("trailing_stop_pct", 0.0) / 100.0
        max_pos_pct = risk.get("max_position_pct", 20) / 100.0
        trend_cfg = self.cfg.get("trend_filter", {})
        trend_enabled = trend_cfg.get("enabled", False)
        trend_fast = trend_cfg.get("ema_fast", 21)
        trend_slow = trend_cfg.get("ema_slow", 50)

        close = df["close"]
        fast = compute_ema(close, ema_fast)
        slow = compute_ema(close, ema_slow)
        rsi = compute_rsi(close, rsi_period)
        atr = compute_atr(df, atr_period)
        high = df["high"]
        low = df["low"]
        if trend_enabled and len(df) > trend_slow + 2:
            tf_ema_fast = compute_ema(close, trend_fast)
            tf_ema_slow = compute_ema(close, trend_slow)
        else:
            tf_ema_fast = tf_ema_slow = None

        warmup = max(ema_slow, rsi_period, atr_period) + 3
        equity = self.initial_equity
        peak_equity = equity
        max_dd = 0.0
        pos = None
        cooldown_until = -1
        trades = []

        for i in range(warmup, len(df)):
            price = float(close.iloc[i])
            if pos:
                entry, side, qty, best, stop, take = pos
                realized = False
                if side == "long":
                    if price <= stop or (trail_pct and price <= best * (1 - trail_pct)):
                        realized = True
                    elif price >= take:
                        realized = True
                    best = max(best, price)
                else:
                    if price >= stop or (trail_pct and price >= best * (1 + trail_pct)):
                        realized = True
                    elif price <= take:
                        realized = True
                    best = min(best, price)
                if realized:
                    pnl = (price - entry) * qty if side == "long" else (entry - price) * qty
                    pnl -= self.fee * abs(qty) * price * 2
                    equity += pnl
                    trades.append({"side": side, "pnl": pnl, "pnl_pct": pnl / (entry * qty) * 100})
                    pos = None
                else:
                    pos = (entry, side, qty, best, stop, take)
                    continue
            if i < len(df) - 1:
                r = float(rsi.iloc[i])
                cross_up = float(fast.iloc[i - 1]) <= float(slow.iloc[i - 1]) and float(fast.iloc[i]) > float(slow.iloc[i])
                cross_dn = float(fast.iloc[i - 1]) >= float(slow.iloc[i - 1]) and float(fast.iloc[i]) < float(slow.iloc[i])
                sig = None
                if cross_up:
                    if not (require_rsi and r >= rsi_overbought):
                        sig = "long"
                elif cross_dn:
                    if not (require_rsi and r <= rsi_oversold):
                        sig = "short"
                if sig and trend_enabled and tf_ema_fast is not None:
                    trend_dir = "long" if tf_ema_fast.iloc[i] > tf_ema_slow.iloc[i] else "short"
                    if trend_dir != sig:
                        sig = None
                if sig and i >= cooldown_until:
                    qty = (equity * max_pos_pct) / price
                    a = float(atr.iloc[i])
                    if use_atr and a > 0:
                        stop = price - atr_stop * a if sig == "long" else price + atr_stop * a
                        take = price + atr_tp * a if sig == "long" else price - atr_tp * a
                    else:
                        stop = price * (1 - sl_pct) if sig == "long" else price * (1 + sl_pct)
                        take = price * (1 + tp_pct) if sig == "long" else price * (1 - tp_pct)
                    pos = (price, sig, qty, price, stop, take)
                    cooldown_until = i + int(cooldown)
            peak_equity = max(peak_equity, equity)
            max_dd = max(max_dd, (peak_equity - equity) / peak_equity)

        return self._stats(df, trades, equity, max_dd)

    def _stats(self, df, trades, equity, max_dd):
        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy,
            "bars": len(df),
            "period_days": round(len(df) * (CANDLE_TF[self.timeframe] or 900) / 86400, 1),
            "trades": len(pnls),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "net_pnl": round(equity - self.initial_equity, 2),
            "total_return_pct": round((equity / self.initial_equity - 1) * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
        }


CANDLE_TF = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def main():
    parser = argparse.ArgumentParser(description="Backtest strategi momentum dengan data historis")
    parser.add_argument("--symbol", default="BTC/USDT:USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--strategy", default="momentum")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--equity", type=float, default=10000.0)
    parser.add_argument("--fee", type=float, default=0.0002)
    parser.add_argument("--mainnet", action="store_true", help="Pakai mainnet (default testnet)")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    bt = Backtester(config, args.symbol, args.timeframe, args.strategy, args.equity, args.fee)
    df = bt.load_data(days=args.days, testnet=not args.mainnet)
    result = bt.run(df)
    for k, v in result.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
