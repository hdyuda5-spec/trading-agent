"""Core decision-layer types: Evidence, Rejection, DecisionVerdict."""

from dataclasses import dataclass, field
from typing import List, Optional


class Status:
    PASS = "PASS"
    REJECT = "REJECT"
    WAIT = "WAIT"


@dataclass
class EvidenceItem:
    """One soft-evidence vote from a single source.

    ``direction`` is LONG/SHORT/None; ``score`` keeps the pre-weight raw
    signed confidence in [-1, 1]; ``contribution`` is ``weight * score``.
    """

    source: str
    signal: Optional[str] = None
    direction: Optional[str] = None
    confidence: float = 0.0
    score: float = 0.0
    weight: float = 1.0
    note: str = ""

    @property
    def contribution(self) -> float:
        return self.weight * self.score

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "signal": self.signal,
            "direction": self.direction,
            "confidence": self.confidence,
            "score": self.score,
            "weight": self.weight,
            "contribution": self.contribution,
            "note": self.note,
        }


@dataclass
class Rejection:
    """A veto with an explicit, explainable reason code."""

    reason_code: str
    reason: str
    category: str = "hard"  # hard | soft | risk | execution

    def as_dict(self) -> dict:
        return {"reason_code": self.reason_code, "reason": self.reason, "category": self.category}


@dataclass
class DecisionVerdict:
    """Structured, explainable outcome of the Decision Engine process."""

    symbol: str
    status: str  # PASS | REJECT | WAIT
    side: Optional[str] = None
    action: Optional[str] = None
    score: float = 0.0
    margin: float = 0.0
    confidence: float = 0.0
    regime: str = "UNCERTAIN"
    regime_note: str = ""
    evidence: List[EvidenceItem] = field(default_factory=list)
    weights: dict = field(default_factory=dict)
    rejection: Optional[Rejection] = None
    reasons: List[str] = field(default_factory=list)
    price: float = 0.0
    atr: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    # convenience
    @property
    def reason_code(self) -> Optional[str]:
        return self.rejection.reason_code if self.rejection else None

    def should_block(self) -> bool:
        return self.status == Status.REJECT

    def evidence_dicts(self) -> List[dict]:
        return [e.as_dict() for e in self.evidence]

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "side": self.side,
            "action": self.action,
            "score": round(self.score, 4),
            "margin": round(self.margin, 4),
            "confidence": round(self.confidence, 4),
            "regime": self.regime,
            "regime_note": self.regime_note,
            "evidence": self.evidence_dicts(),
            "weights": self.weights,
            "rejection": self.rejection.as_dict() if self.rejection else None,
            "reasons": list(self.reasons),
            "price": self.price,
            "atr": self.atr,
        }

    def to_decision_shape(self) -> dict:
        """Legacy bot-compatible decision dict for consumers that expect the
        old ``{action, confidence, reason[]}`` shape."""
        return {
            "action": self.action,
            "confidence": round(self.confidence, 4),
            "reason": [
                *(self.reasons or []),
                *(f"REJECT {self.reason_code}: {self.rejection.reason}" if self.rejection else []),
            ],
        }