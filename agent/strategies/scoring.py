"""Weighted confidence scoring for the screening pipeline.

Replaces the old hard-reject filter chain with weighted confidence scoring.
Only five conditions may hard-reject a candidate::

    1. insufficient exchange balance
    2. exchange unavailable
    3. daily max drawdown exceeded
    4. invalid order (market/precision/cost — not executable)
    5. risk violation (exposure / open-position / policy limits)

Everything else (RSI, Whale, Volume, Funding, Open Interest, News,
Sentiment, extreme moves, losing streaks, market regime) is converted into
a *confidence modifier* — it bends the final confidence but never vetoes.

The pipeline is strictly three-stage:

    Decision Engine (this module)  -> {action, confidence, reason[]}
        -> Risk Engine              -> position size   (agent.core.risk)
        -> Execution Engine         -> order executable  (ExecutionService)
"""

import logging
import time

from agent.core.utils import is_valid_atr
from agent.features import build_feature_engine

logger = logging.getLogger("trading-agent")

TRACE_PREFIX = "[TRACE]"

# Factor weight table — sums to 1.0. Liquidity & Structure are the heaviest.
WEIGHTS = {
    "liquidity": 0.25,
    "structure": 0.20,
    "trend": 0.15,
    "volume": 0.15,
    "funding": 0.10,
    "open_interest": 0.10,
    "whale": 0.05,
}

# Feature sources for each weighted factor. ``structure`` prefers the SMC
# market-structure signal and falls back to the S/R + pattern feature.
_FACTOR_FEATURE = {
    "liquidity": "smart_money_liquidity",
    "structure": "market_structure",
    "trend": "trend",
    "volume": "volume",
    "funding": "funding",
    "open_interest": "open_interest",
    "whale": "whale",
}

# All features the decision engine needs (df-only + market-data features).
SCORING_FEATURES = [
    "trend",
    "volatility",
    "volume",
    "structure",
    "market_structure",
    "smart_money_liquidity",
    "whale",
    "sentiment",
    "funding",
    "open_interest",
]

_BULLISH = {"bullish", "liquid", "rising"}
_BEARISH = {"bearish", "illiquid", "falling"}


class ScreenerScorer:
    """Weighted-confidence Decision Engine for screener candidates."""

    def __init__(self, config, exchange):
        self.config = config
        self.exchange = exchange
        self.engine = build_feature_engine(
            market_data=exchange, config=config, enabled=list(SCORING_FEATURES)
        )
        self.weights = self._load_weights()
        self.total_weight = sum(self.weights.values())
        sc = config.get("screener", {}) or {}
        scoring = sc.get("scoring", {}) or {}
        self.min_confidence = float(scoring.get("min_confidence", 0.60))
        self.min_margin = float(scoring.get("min_margin", 0.10))
        self.rsi_bonus = float(scoring.get("rsi_bonus", 0.03))
        self.rsi_max_penalty = float(scoring.get("rsi_max_penalty", 0.12))
        self.rsi_tolerance = float(scoring.get("rsi_tolerance", 10.0))
        self.sentiment_adjust = float(scoring.get("sentiment_max_adjust", 0.10))
        self.news_adjust = float(scoring.get("news_max_adjust", 0.10))
        self.streak_penalty = float(scoring.get("streak_penalty", 0.08))
        self.regime_penalty = float(scoring.get("regime_penalty", 0.10))
        self.extreme_penalty = float(scoring.get("extreme_move_max_penalty", 0.20))
        self.max_confidence = float(scoring.get("max_confidence", 0.97))
        self.max_buy_chg = float(scoring.get("max_buy_chg_pct", 0) or 0)

    def _load_weights(self) -> dict:
        cfg = (self.config.get("screener", {}) or {}).get("scoring", {}) or {}
        weights = dict(WEIGHTS)
        overrides = cfg.get("weights", {}) or {}
        weights.update({k: max(0.0, float(v)) for k, v in overrides.items()})
        return weights

    # -- public API -------------------------------------------------------

    def evaluate(self, symbol, df, chg=None, news=None, losing_streak=False, market_regime=None) -> dict:
        """Full verdict with per-factor scores, modifiers and trace reasons."""
        fs = self.engine.compute(symbol, df, include_market=True)
        price = float(df["close"].iloc[-1])
        atr = self._atr(fs)

        factors, reasons = self._factor_scores(fs)
        direction = sum(self.weights[k] * factors[k] for k in self.weights)
        side = "LONG" if direction > 0 else ("SHORT" if direction < 0 else None)

        rsi = self._rsi(fs)
        modifiers = {}
        notes = []
        if side:
            mod, note = self._rsi_modifier(side, rsi)
            modifiers["rsi"] = mod
            if note:
                notes.append(note)
            mod, note = self._chg_modifier(chg)
            modifiers["chg"] = mod
            if note:
                notes.append(note)
            mod, note = self._sentiment_modifier(fs, side)
            modifiers["sentiment"] = mod
            if note:
                notes.append(note)
            mod, note = self._news_modifier(news)
            modifiers["news"] = mod
            if note:
                notes.append(note)
            mod, note = self._streak_modifier(losing_streak)
            modifiers["streak"] = mod
            if note:
                notes.append(note)
            mod, note = self._regime_modifier(side, market_regime)
            modifiers["regime"] = mod
            if note:
                notes.append(note)

        base = 0.5 + 0.5 * abs(direction)
        confidence = min(self.max_confidence, max(0.05, base + sum(modifiers.values())))

        rejected = False
        reject_reason = None
        action = None
        if side is None or abs(direction) < self.min_margin:
            rejected = True
            reject_reason = f"margin {direction:+.2f} < {self.min_margin}"
        elif side == "LONG" and self._pump_chase_reject(chg):
            rejected = True
            reject_reason = f"pump-chase chg {float(chg):+.2f}% > {self.max_buy_chg}%"
        elif confidence < self.min_confidence:
            rejected = True
            reject_reason = f"confidence {confidence:.2f} < {self.min_confidence}"
        else:
            action = "BUY" if side == "LONG" else "SELL"

        reason = reasons + notes
        return {
            "symbol": symbol,
            "price": round(price, 8),
            "atr": round(atr, 8) if is_valid_atr(atr) else None,
            "action": action,
            "side": side,
            "confidence": round(confidence, 4),
            "direction": round(direction, 4),
            "margin": round(abs(direction), 4),
            "factors": {k: round(v, 4) for k, v in factors.items()},
            "modifiers": {k: round(v, 4) for k, v in modifiers.items()},
            "reason": reason,
            "rsi": rsi,
            "rejected": rejected,
            "reject_reason": reject_reason,
            "features": fs.snapshot(),
        }

    def decide(self, symbol, df, **kwargs) -> dict:
        """Canonical Decision shape: ``{action, confidence, reason[]}``."""
        verdict = self.evaluate(symbol, df, **kwargs)
        return self.to_decision(verdict)

    @staticmethod
    def to_decision(verdict: dict) -> dict:
        return {
            "action": verdict.get("action"),
            "confidence": verdict.get("confidence", 0.0),
            "reason": [str(r) for r in verdict.get("reason", [])],
        }

    # -- factor scoring ---------------------------------------------------

    def _factor_scores(self, fs) -> tuple:
        factors = {}
        reasons = []
        for factor, weight in self.weights.items():
            fr = fs.get(_FACTOR_FEATURE[factor])
            source = _FACTOR_FEATURE[factor]
            if factor == "structure":
                if fr is None or fr.signal == "neutral":
                    fr = fs.get("structure")
                    source = "structure"
            s, note = self._factor_direction(fr, source)
            factors[factor] = s
            if note:
                reasons.append(note)
        return factors, reasons

    def _factor_direction(self, fr, source) -> tuple:
        if fr is None or fr.signal == "neutral" or fr.signal == "unavailable":
            return 0.0, f"{source}: neutral"
        conf = min(1.0, float(fr.confidence))
        if fr.signal in _BULLISH:
            return conf, f"{source}: {fr.signal} +{conf:.2f}"
        if fr.signal in _BEARISH:
            return -conf, f"{source}: {fr.signal} -{conf:.2f}"
        return 0.0, f"{source}: {fr.signal}"

    # -- confidence modifiers ---------------------------------------------

    def _rsi(self, fs) -> float:
        trend = fs.get("trend")
        if trend is None:
            return 50.0
        try:
            return float(trend.metadata.get("rsi") or 50.0)
        except (TypeError, ValueError):
            return 50.0

    def _rsi_modifier(self, side, rsi):
        sc = self.config.get("screener", {}) or {}
        if not sc.get("rsi_confirmation", True):
            return 0.0, None
        if side == "LONG":
            lo, hi = sc.get("rsi_long_range", [40, 75])
        else:
            lo, hi = sc.get("rsi_short_range", [25, 60])
        lo, hi = float(lo), float(hi)
        if lo <= rsi <= hi:
            return self.rsi_bonus, f"rsi {rsi:.1f} dalam range {side}"
        if rsi > hi + self.rsi_tolerance:
            return -self.rsi_max_penalty, f"rsi {rsi:.1f} terlalu tinggi untuk {side}"
        if rsi < lo - self.rsi_tolerance:
            return -self.rsi_max_penalty * 0.7, f"rsi {rsi:.1f} terlalu rendah untuk {side}"
        return -self.rsi_max_penalty * 0.5, f"rsi {rsi:.1f} di luar range {side}"

    def _pump_chase_reject(self, chg):
        if self.max_buy_chg <= 0 or chg is None:
            return False
        return float(chg) > self.max_buy_chg

    def _chg_modifier(self, chg):
        sc = self.config.get("screener", {}) or {}
        max_ext = float(sc.get("max_extended_pct", 0) or 0)
        if max_ext <= 0 or chg is None:
            return 0.0, None
        chg = abs(float(chg))
        if chg <= max_ext:
            return 0.0, None
        excess = (chg - max_ext) / max_ext if max_ext else 1.0
        penalty = min(self.extreme_penalty, 0.05 + excess * 0.05)
        return -penalty, f"move {float(chg):+.2f}% ekstrem"

    def _sentiment_modifier(self, fs, side):
        fr = fs.get("sentiment")
        if fr is None or fr.signal == "neutral":
            return 0.0, None
        agrees = (fr.signal == "bullish" and side == "LONG") or (fr.signal == "bearish" and side == "SHORT")
        mod = self.sentiment_adjust * min(1.0, float(fr.confidence)) if agrees else -self.sentiment_adjust
        return mod, f"sentiment {'searah' if agrees else 'melawan'} {fr.signal}"

    def _news_modifier(self, news):
        if not news:
            return 0.0, None
        scores = []
        if isinstance(news, dict):
            news = [news]
        for item in news:
            if not isinstance(item, dict):
                continue
            score = item.get("sentiment", item.get("score"))
            if score is None:
                continue
            try:
                scores.append(max(-1.0, min(1.0, float(score))))
            except (TypeError, ValueError):
                continue
        if not scores:
            return 0.0, "news tidak tersedia"
        avg = sum(scores) / len(scores)
        mod = self.news_adjust * avg
        return mod, f"news sentiment {avg:+.2f}"

    def _streak_modifier(self, losing_streak):
        if losing_streak:
            return -self.streak_penalty, "pola kalah beruntun"
        return 0.0, None

    def _regime_modifier(self, side, market_regime):
        if not market_regime:
            return 0.0, None
        total, with_data = market_regime
        reg = (self.config.get("whale", {}) or {}).get("market_regime", {}) or {}
        if not reg.get("enabled", False):
            return 0.0, None
        threshold = float(reg.get("net_sell_threshold_usdt", 0))
        min_symbols = int(reg.get("min_symbols", 5))
        if with_data < min_symbols:
            return 0.0, None
        if side == "LONG" and total <= -threshold:
            return -self.regime_penalty, f"regime whale net sell {total:+.0f} USDT"
        if side == "SHORT" and total >= threshold:
            return -self.regime_penalty, f"regime whale net buy {total:+.0f} USDT"
        return 0.0, None

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _atr(fs):
        vola = fs.get("volatility")
        if vola is None:
            return None
        try:
            return float(vola.metadata.get("atr"))
        except (TypeError, ValueError):
            return None

    def log_trace(self, symbol, verdict, outcome, detail=""):
        """Emit the structured Decision Trace line for the verdict."""
        factors = " ".join(f"{k}={v:+.2f}" for k, v in verdict.get("factors", {}).items())
        mods = " ".join(
            f"{k}={v:+.2f}" for k, v in verdict.get("modifiers", {}).items() if abs(v) > 1e-9
        )
        parts = [
            f"{TRACE_PREFIX} {symbol} {outcome}",
            f"action={verdict.get('action')}",
            f"conf={verdict.get('confidence', 0.0):.2f}/{self.min_confidence:.2f}",
            f"dir={verdict.get('direction', 0.0):+.2f}",
        ]
        if factors:
            parts.append(f"factors {{{factors}}}")
        if mods:
            parts.append(f"mods {{{mods}}}")
        if detail:
            parts.append(f"detail={detail}")
        if verdict.get("reject_reason"):
            parts.append(f"reject={verdict['reject_reason']}")
        reasons = " | ".join(str(r) for r in verdict.get("reason", []))
        if reasons:
            parts.append(f"reason[{reasons}]")
        log_fn = logger.warning if verdict.get("rejected") or outcome != "ACCEPT" else logger.info
        log_fn(" ".join(parts))


# Hard-reject gate -----------------------------------------------------------

_REASON_CATEGORY = {
    "market tidak ditemukan": "invalid_order",
    "spread/fee": "invalid_order",
    "qty di bawah presisi pasar": "invalid_order",
    "minCost": "invalid_order",
    "minAmt": "invalid_order",
    "max open positions reached": "risk_violation",
    "daily loss limit reached": "risk_violation",
    "max total exposure reached": "risk_violation",
}


def classify_order_reason(reason: str) -> str:
    """Map an execution preflight failure onto a hard-reject category."""
    reason = str(reason or "")
    if "budget" in reason:
        return "insufficient_balance"
    for key, category in _REASON_CATEGORY.items():
        if key in reason:
            return category
    return "invalid_order"


class ScreeningGate:
    """The only five conditions that hard-reject a screened candidate.

    Every other rejection in the pipeline is a *confidence* rejection from
    ``ScreenerScorer`` (logged as a Decision Trace), not a hard gate.
    """

    def __init__(self, config, risk, execution, exchange, portfolio):
        self.config = config
        self.risk = risk
        self.execution = execution
        self.exchange = exchange
        self.portfolio = portfolio
        self._health = {"ok": True, "at": 0.0}
        self._health_ttl = 60.0

    def exchange_available(self) -> bool:
        now = time.time()
        if now - self._health["at"] < self._health_ttl:
            return self._health["ok"]
        try:
            ok = bool(self.exchange.check_health())
        except Exception:
            ok = False
        self._health = {"ok": ok, "at": now}
        return ok

    def check_global(self, equity: float):
        """Balance + drawdown gates (run once per auto-trade pass)."""
        min_eq = float(self.config.get("risk", {}).get("min_equity_usdt", 0) or 0)
        if equity <= 0 or (min_eq > 0 and equity < min_eq):
            return False, "insufficient_balance", f"equity {equity:.2f} < min {min_eq}"
        if self.portfolio.halted or self.risk.daily_loss_exceeded(equity):
            return False, "max_drawdown", f"equity {equity:.2f} melewati daily loss limit"
        return True, None, None

    def check_order(self, symbol, side, price, atr, equity, positions):
        """Invalid-order + risk-violation gates per candidate."""
        try:
            ok, equity_eff, reason = self.execution.screen_trade_ok(
                symbol, side, price, atr, equity, positions
            )
        except Exception as e:
            return False, "invalid_order", f"preflight error: {e}", None
        if not ok:
            return False, classify_order_reason(reason), reason, None
        return True, None, None, equity_eff
