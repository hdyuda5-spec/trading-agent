"""TradeTicket — the only object the execution layer may act on (Phase 13).

A ticket is born from a PASS verdict + risk sizing, expires after
``ticket_ttl_seconds``, and carries the full evidence/reason trail so any
execution is auditable end-to-end (ticket → order id → fill → journal).
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from agent.decision.types import DecisionVerdict
from agent.core.utils import is_valid_atr


@dataclass
class TradeTicket:
    ticket_id: str
    symbol: str
    side: str          # LONG / SHORT
    strategy: str
    entry: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_pct: float = 0.0
    risk_amount: float = 0.0
    position_size: Optional[float] = None
    leverage: float = 1.0
    notional: Optional[float] = None
    atr: Optional[float] = None
    rr: Optional[float] = None
    score: float = 0.0
    confidence: float = 0.0
    regime: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    evidence: List[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    equity: Optional[float] = None   # effective equity used for sizing
    source: Optional[str] = None     # "screener" | "signal_service" | ...
    decision_status: Optional[str] = None  # PASS | REJECT | WAIT from the Decision Engine
    status: str = "NEW"            # NEW | FILLED | CANCELLED | EXPIRED | REJECTED
    order_id: Optional[str] = None
    exchange_order_id: Optional[str] = None
    order_type: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    ttl_seconds: Optional[float] = None

    @classmethod
    def new(cls, symbol: str, side: str, strategy: str, entry: float,
            *, stop_loss=None, take_profit=None, risk_pct=0.0, risk_amount=0.0,
            position_size=None, leverage=1.0, atr=None, rr=None,
            score=0.0, confidence=0.0, regime=None, reasons=None,
            evidence=None, ttl_seconds=None, metadata=None, ticket_id=None,
            equity=None, source=None, decision_status=None) -> "TradeTicket":
        ttl = ttl_seconds if ttl_seconds is not None else 300.0
        return cls(
            ticket_id=ticket_id or uuid.uuid4().hex[:12].upper(),
            symbol=symbol,
            side=side,
            strategy=strategy,
            entry=float(entry),
            stop_loss=float(stop_loss) if stop_loss is not None else None,
            take_profit=float(take_profit) if take_profit is not None else None,
            risk_pct=risk_pct,
            risk_amount=risk_amount,
            position_size=position_size,
            leverage=float(leverage),
            notional=(float(position_size) * float(entry)) if position_size else None,
            atr=float(atr) if is_valid_atr(atr) else None,
            rr=float(rr) if rr is not None else None,
            score=score,
            confidence=confidence,
            regime=regime,
            reasons=list(reasons or []),
            evidence=list(evidence or []),
            metadata=dict(metadata or {}),
            equity=float(equity) if equity is not None else None,
            source=source,
            decision_status=decision_status,
            ttl_seconds=ttl,
            expires_at=time.time() + ttl,
        )

    # -- lifecycle -------------------------------------------------------

    def expired(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if self.expires_at is None:
            return False
        return now >= self.expires_at

    def is_valid(self, now: Optional[float] = None) -> bool:
        """Execution gate: only NEW, well-formed, non-expired tickets may act."""
        if not self.ticket_id:
            return False
        if not self.symbol:
            return False
        if self.side not in ("LONG", "SHORT"):
            return False
        try:
            if not (float(self.entry) > 0):
                return False
        except (TypeError, ValueError):
            return False
        if self.status != "NEW":
            return False
        if self.expired(now):
            return False
        return True

    def mark_filled(self, order_id: str, exchange_order_id: Optional[str] = None,
                    order_type: Optional[str] = None) -> None:
        self.status = "FILLED"
        self.order_id = order_id
        self.exchange_order_id = exchange_order_id
        self.order_type = order_type

    def mark(self, status: str, reason: Optional[str] = None) -> None:
        self.status = status
        if reason:
            self.reasons.append(reason)

    # -- serialization ---------------------------------------------------

    def signal_dict(self) -> dict:
        """Legacy-shaped signal for ``OrderManager.open_position(signal)``.

        Keeps existing consumers working while carrying the ticket trail.
        """
        risk = {
            "entry": self.entry,
            "sl": self.stop_loss,
            "tp": self.take_profit,
            "atr": self.atr,
            "rr": self.rr,
        }
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "side": self.side,
            "action": "BUY" if self.side == "LONG" else "SELL",
            "price": self.entry,
            "confidence": self.confidence,
            "reason": list(self.reasons),
            "risk": {k: v for k, v in risk.items() if v is not None},
            "ticket_id": self.ticket_id,
            "metadata": dict(self.metadata),
        }

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "symbol": self.symbol,
            "side": self.side,
            "strategy": self.strategy,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "risk_pct": self.risk_pct,
            "risk_amount": self.risk_amount,
            "position_size": self.position_size,
            "leverage": self.leverage,
            "notional": self.notional,
            "atr": self.atr,
            "rr": self.rr,
            "score": self.score,
            "confidence": self.confidence,
            "regime": self.regime,
            "reasons": list(self.reasons),
            "evidence": list(self.evidence),
            "equity": self.equity,
            "source": self.source,
            "decision_status": self.decision_status,
            "status": self.status,
            "order_id": self.order_id,
            "exchange_order_id": self.exchange_order_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @staticmethod
    def from_verdict(verdict: DecisionVerdict, strategy: str,
                     position_size: Optional[float] = None, risk_pct: float = 0.0,
                     risk_amount: float = 0.0, leverage: float = 1.0,
                     ttl_seconds: Optional[float] = None) -> "TradeTicket":
        return TradeTicket.new(
            verdict.symbol, verdict.side, strategy, verdict.price,
            stop_loss=None, take_profit=None, risk_pct=risk_pct, risk_amount=risk_amount,
            position_size=position_size, leverage=leverage, atr=verdict.atr,
            score=verdict.score, confidence=verdict.confidence, regime=verdict.regime,
            reasons=list(verdict.reasons), evidence=verdict.evidence_dicts(),
            ttl_seconds=ttl_seconds, metadata={"decision_status": verdict.status},
        )