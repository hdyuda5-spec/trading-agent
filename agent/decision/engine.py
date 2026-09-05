"""Unified Decision Engine (Phases 3-6).

Pipeline:

    features + strategy signal + advisory + news
        ├─ HardVeto  (if any veto → REJECT, explainable)
        ├─ Regime    (market regime label + safety multiplier)
        ├─ Evidence  (soft votes → weighted score)
        └─ Decision  (PASS / REJECT / WAIT)  with reason_code

The engine is a *gate + scorer*: it produces an explainable verdict and, when
PASS, everything downstream (risk sizing → TradeTicket → execution) can
proceed. It never touches the exchange and never places orders.
"""

from typing import Any, Dict, List, Optional

from agent.core.utils import is_valid_atr
from agent.decision.evidence import EvidenceCollector, HardVeto, to_direction
from agent.decision.regime import MarketRegimeEngine
from agent.decision.types import DecisionVerdict, EvidenceItem, Rejection, Status
from agent.decision.weights import load_min_margin, load_thresholds, load_weights, total_weight


class UnifiedDecisionEngine:
    def __init__(self, config=None, feature_engine=None, risk=None, portfolio=None, exchange=None):
        self.config = config or {}
        self.feature_engine = feature_engine
        self.weights = load_weights(self.config)
        self.thresholds = load_thresholds(self.config)
        self.min_margin = load_min_margin(self.config)
        self.regime_engine = MarketRegimeEngine(self.config)
        self.veto = HardVeto(self.config, risk=risk, portfolio=portfolio, exchange=exchange)
        self.collector = EvidenceCollector(self.weights)

    # -- public ----------------------------------------------------------

    def assess(self, symbol: str, df: Any, features=None, advisory: Optional[dict] = None,
               news=None, strategy_side: Optional[str] = None, signal_conf: Optional[float] = None,
               equity: Optional[float] = None, positions: Optional[List[dict]] = None,
               price: Optional[float] = None, sl: Optional[float] = None,
               tp: Optional[float] = None, atr: Optional[float] = None) -> DecisionVerdict:
        if df is not None and len(df) >= 5:
            close = df["close"]
            price = float(close.iloc[-1]) if price is None else float(price)
        price = float(price or 0.0)

        regime = self.regime_engine.detect(df, atr)
        evidence = self.collector.collect(
            features, advisory=advisory, news=news,
            strategy_side=strategy_side, signal_conf=signal_conf,
            rsi=self._rsi(features),
        )

        w_total = total_weight(self.weights)
        score = sum(e.contribution for e in evidence)
        margin = score / w_total if w_total > 0 else 0.0
        if margin > 0:
            side = "LONG"
        elif margin < 0:
            side = "SHORT"
        else:
            side = strategy_side  # neutral evidence: defer to strategy direction

        status = Status.WAIT
        rejection = None
        if side is None:
            status = Status.WAIT
        else:
            threshold = self.thresholds["long"] if side == "LONG" else self.thresholds["short"]
            if abs(margin) < self.min_margin:
                status = Status.WAIT
                rejection = Rejection("NEUTRAL_SIGNAL",
                                      f"margin {margin:+.2f} < min_margin {self.min_margin}", "soft")
            elif score < threshold:
                status = Status.REJECT
                rejection = Rejection("SCORE_TOO_LOW",
                                      f"score {score:+.2f} < threshold {threshold:+.2f}", "soft")
            else:
                status = Status.PASS

        # hard vetoes run last and win over any soft outcome
        ok, hrej = self.veto.check(symbol, side, price, equity, positions, sl=sl, tp=tp, atr=atr)
        if not ok:
            status = Status.REJECT
            rejection = hrej

        confidence = min(0.95, 0.5 + 0.5 * abs(margin))
        reasons = [e.note for e in evidence if e.contribution != 0.0]
        if rejection is not None and rejection.reason:
            reasons.append(f"REJECT {rejection.reason_code}: {rejection.reason}")

        return DecisionVerdict(
            symbol=symbol,
            status=status,
            side=side,
            action="BUY" if side == "LONG" else ("SELL" if side == "SHORT" else None),
            score=round(score, 4),
            margin=round(margin, 4),
            confidence=round(confidence, 4),
            regime=regime["regime"],
            regime_note=regime.get("reason", ""),
            evidence=evidence,
            weights=dict(self.weights),
            rejection=rejection,
            reasons=reasons,
            price=price,
            atr=atr,
            metadata={"regime": regime},
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _rsi(features) -> Optional[float]:
        if features is None:
            return None
        trend = features.get("trend") if hasattr(features, "get") else None
        if trend is None:
            return None
        try:
            return float(trend.metadata.get("rsi"))
        except (TypeError, ValueError):
            return None