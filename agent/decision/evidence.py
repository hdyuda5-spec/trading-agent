"""Evidence system — HARD VETO vs SOFT EVIDENCE (Phase 4).

Hard vetoes are conditions that must never trade: daily loss, exposure,
invalid SL/TP, invalid price, halted/exchange unavailable. Soft evidence
(EMA/RSI/trend/whale/CVD/orderbook/pattern/AI/news) contributes *score*, never
an outright block — a soft failure only lowers the decision score.
"""

from typing import List, Optional, Tuple

from agent.core.utils import is_valid_atr
from agent.decision.regime import UNCERTAIN
from agent.decision.types import EvidenceItem, Rejection


class HardVeto:
    def __init__(self, config=None, risk=None, portfolio=None, exchange=None):
        self.config = config or {}
        self.risk = risk
        self.portfolio = portfolio
        self.exchange = exchange

    def check(self, symbol, side, price, equity, positions=None,
              sl=None, tp=None, atr=None) -> Tuple[bool, Optional[Rejection]]:
        """Returns (ok, rejection). No exchange/network calls by default."""

        if price is None or price <= 0:
            return False, Rejection("INVALID_PRICE", f"harga entry {price} tidak valid")
        if equity is None or equity <= 0:
            return False, Rejection("NO_EQUITY", "tidak ada equity / balance 0")
        if isinstance(sl, (int, float)) and sl <= 0:
            return False, Rejection("INVALID_SL", f"stop loss {sl} tidak valid")
        if isinstance(tp, (int, float)) and tp <= 0:
            return False, Rejection("INVALID_TP", f"take profit {tp} tidak valid")
        if sl is not None and tp is not None:
            if side == "LONG" and not (sl < price < tp):
                return False, Rejection("INVALID_LEVELS", f"LONG memerlukan SL<entry<TP (sl={sl}, tp={tp})")
            if side == "SHORT" and not (tp < price < sl):
                return False, Rejection("INVALID_LEVELS", f"SHORT memerlukan TP<entry<SL (sl={sl}, tp={tp})")

        if self.risk is not None:
            try:
                if self.risk.daily_loss_exceeded(equity):
                    return False, Rejection("DAILY_LOSS_LIMIT", "daily loss limit tercapai", "risk")
                if self.risk.below_min_equity(equity):
                    return False, Rejection("MIN_EQUITY", f"equity {equity:.2f} di bawah minimal", "risk")
            except Exception:
                pass

        if self.portfolio is not None and getattr(self.portfolio, "halted", False):
            return False, Rejection("TRADING_HALTED", "trading di-halt (risk guard)", "risk")

        if self.exchange is not None:
            try:
                if not self.exchange.check_health():
                    return False, Rejection("EXCHANGE_UNAVAILABLE", "exchange tidak sehat", "execution")
            except Exception:
                return False, Rejection("EXCHANGE_UNAVAILABLE", "gagal cek health exchange", "execution")

        return True, None


_DIRECTION_MAP = {
    "bullish": "LONG",
    "bearish": "SHORT",
    "liquid": "LONG",
    "illiquid": "SHORT",
    "rising": "LONG",
    "falling": "SHORT",
    "LONG": "LONG",
    "SHORT": "SHORT",
    "up": "LONG",
    "down": "SHORT",
}


def to_direction(signal) -> Optional[str]:
    if signal is None:
        return None
    if isinstance(signal, str):
        return _DIRECTION_MAP.get(signal.strip().lower())
    return None


def _conf(value, default=0.0) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, c))


def feature_score(feature_result, direction) -> float:
    """Signed confidence in [-1, 1] from a feature result and its direction."""
    conf = _conf(getattr(feature_result, "confidence", None)) if feature_result is not None else 0.0
    return conf if direction else (conf if direction is None else 0.0)


class EvidenceCollector:
    """Turns features/inputs into a list of weighted ``EvidenceItem`` votes."""

    WEIGHT_KEYS = (
        "technical",
        "trend",
        "momentum",
        "liquidity",
        "order_flow",
        "whale",
        "pattern",
        "ai",
        "news",
    )

    def __init__(self, weights: dict):
        self.weights = {k: max(0.0, float(weights.get(k, 0.0))) for k in self.WEIGHT_KEYS}

    def _item(self, source, feature_result, note=None) -> EvidenceItem:
        direction = to_direction(getattr(feature_result, "signal", None))
        conf = _conf(getattr(feature_result, "confidence", None))
        score = conf if direction == "LONG" else (-conf if direction == "SHORT" else 0.0)
        return EvidenceItem(
            source=source,
            signal=getattr(feature_result, "signal", None) if feature_result is not None else None,
            direction=direction,
            confidence=conf,
            score=score,
            weight=self.weights.get(source, 0.0),
            note=note or f"{source}: {getattr(feature_result, 'signal', 'n/a')}",
        )

    def collect(self, fs, advisory=None, news=None, strategy_side=None,
                rsi=None, signal_conf=None) -> List[EvidenceItem]:
        items: List[EvidenceItem] = []

        if fs is not None:
            for source, fname in (
                ("liquidity", "smart_money_liquidity"),
                ("trend", "trend"),
            ):
                fr = fs.get(fname) if hasattr(fs, "get") else None
                if fr is not None:
                    items.append(self._item(source, fr))

            structure = fs.get("market_structure") if hasattr(fs, "get") else None
            if structure is not None and str(getattr(structure, "signal", "")).lower() != "neutral":
                items.append(self._item("technical", structure))
            for fname in ("order_blocks", "fvg"):
                fr = fs.get(fname) if hasattr(fs, "get") else None
                if fr is not None and str(getattr(fr, "signal", "")).lower() not in ("neutral", "none"):
                    items.append(self._item("pattern", fr))
            whale_f = fs.get("whale") if hasattr(fs, "get") else None
            if whale_f is not None:
                items.append(self._item("whale", whale_f))
            order_flow = fs.get("liquidity") if hasattr(fs, "get") else None
            if order_flow is not None:
                # ``liquidity`` (orderbook imbalance) and ``smart_money_liquidity``
                # (CVD/OBV/sweeps) are distinct features; never double-count the
                # same object under two sources.
                sml = fs.get("smart_money_liquidity") if hasattr(fs, "get") else None
                if order_flow is not sml and str(getattr(order_flow, "signal", "")).lower() not in ("neutral",):
                    items.append(self._item("order_flow", order_flow))

        # momentum vote from the strategy's own direction + RSI confirmation
        if strategy_side:
            conf = _conf(signal_conf, 0.5) if signal_conf is not None else 0.5
            items.append(EvidenceItem(
                source="momentum",
                signal=strategy_side,
                direction=strategy_side,
                confidence=conf,
                score=conf if strategy_side == "LONG" else -conf,
                weight=self.weights.get("momentum", 0.0),
                note=f"momentum: signal {strategy_side} (conf {conf:.0%})",
            ))
        elif rsi is not None:
            direction = "LONG" if rsi >= 50 else "SHORT"
            conf = min(1.0, abs(rsi - 50) / 50.0)
            items.append(EvidenceItem(
                source="momentum", signal="rsi", direction=direction, confidence=conf,
                score=conf if direction == "LONG" else -conf,
                weight=self.weights.get("momentum", 0.0), note=f"rsi: {rsi:.1f}",
            ))

        # AI advisory (never the sole reason: capped by its weight)
        if isinstance(advisory, dict):
            direction = to_direction(advisory.get("trend"))
            conf = _conf(advisory.get("confidence"))
            items.append(EvidenceItem(
                source="ai", signal=str(advisory.get("trend")), direction=direction,
                confidence=conf, score=conf if direction == "LONG" else (-conf if direction == "SHORT" else 0.0),
                weight=self.weights.get("ai", 0.0),
                note=f"ai_advisory: {advisory.get('trend')} (conf {conf:.0%})",
            ))

        # news — soft, provider-agnostic list of {sentiment: -1..1}
        news = news or []
        if isinstance(news, dict):
            news = [news]
        scores = []
        for item in news:
            if not isinstance(item, dict):
                continue
            s = item.get("sentiment", item.get("score"))
            try:
                scores.append(max(-1.0, min(1.0, float(s))))
            except (TypeError, ValueError):
                continue
        if scores:
            avg = sum(scores) / len(scores)
            items.append(EvidenceItem(
                source="news", signal="sentiment",
                direction="LONG" if avg > 0 else ("SHORT" if avg < 0 else None),
                confidence=abs(avg), score=avg,
                weight=self.weights.get("news", 0.0),
                note=f"news sentiment {avg:+.2f}",
            ))

        return items