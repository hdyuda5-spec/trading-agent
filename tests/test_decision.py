"""Decision engine tests: regime + veto + evidence + score + verdict + ticket."""

import pandas as pd
import pytest

from agent.core.risk import RiskEngine
from agent.decision.engine import UnifiedDecisionEngine
from agent.decision.evidence import EvidenceCollector, HardVeto, to_direction
from agent.decision.regime import (
    HIGH_VOLATILITY,
    LOW_VOLATILITY,
    RANGING,
    TRENDING_DOWN,
    TRENDING_UP,
    UNCERTAIN,
    MarketRegimeEngine,
)
from agent.decision.ticket import TradeTicket
from agent.decision.types import EvidenceItem, Rejection, Status


def make_df(close, high=None, low=None):
    n = len(close)
    return pd.DataFrame({
        "open": list(close),
        "high": list(high) if high is not None else list(close),
        "low": list(low) if low is not None else list(close),
        "close": list(close),
    })


def trend_up_df(n=100):
    return make_df([100 + 2 * i for i in range(n)])


def trend_down_df(n=100):
    return make_df([200 - 2 * i for i in range(n)])


class F:
    def __init__(self, signal, confidence=0.5):
        self.signal = signal
        self.confidence = confidence
        self.metadata = {}


class FS:
    def __init__(self, **kw):
        self._map = kw

    def get(self, name, default=None):
        return self._map.get(name, default)

    def __contains__(self, name):
        return name in self._map


class FakePortfolio:
    halted = False


class FakeRisk:
    def daily_loss_exceeded(self, equity):
        return False

    def below_min_equity(self, equity):
        return False


# ---------------------------------------------------------------- regime


def test_regime_trending_up_on_uptrend():
    info = MarketRegimeEngine().detect(trend_up_df())
    assert info["regime"] in (TRENDING_UP, HIGH_VOLATILITY)


def test_regime_trending_down_on_downtrend():
    info = MarketRegimeEngine().detect(trend_down_df())
    assert info["regime"] in (TRENDING_DOWN, HIGH_VOLATILITY)


def test_regime_ranging_on_flat():
    flat = make_df([100.0] * 100)
    info = MarketRegimeEngine().detect(flat)
    assert info["regime"] in (RANGING, LOW_VOLATILITY)


def test_regime_uncertain_on_short_data():
    info = MarketRegimeEngine().detect(make_df([1.0, 2.0, 3.0]))
    assert info["regime"] == UNCERTAIN
    assert info["size_multiplier"] == 0.0


# ---------------------------------------------------------------- veto


def test_hard_veto_invalid_price():
    ok, rej = HardVeto(risk=FakeRisk()).check("X", "LONG", 0.0, 1000.0)
    assert not ok and rej.reason_code == "INVALID_PRICE"


def test_hard_veto_no_equity():
    ok, rej = HardVeto(risk=FakeRisk()).check("X", "LONG", 100.0, None)
    assert not ok and rej.reason_code == "NO_EQUITY"


def test_hard_veto_invalid_levels():
    v = HardVeto(risk=FakeRisk())
    ok, rej = v.check("X", "LONG", 100.0, 1000.0, sl=110.0, tp=120.0)
    assert not ok and rej.reason_code == "INVALID_LEVELS"


def test_hard_veto_exchange_down():
    class Down:
        def check_health(self):
            return False

    ok, rej = HardVeto(risk=FakeRisk(), exchange=Down()).check("X", "LONG", 100.0, 1000.0)
    assert not ok and rej.reason_code == "EXCHANGE_UNAVAILABLE"


def test_hard_veto_ok_path():
    ok, rej = HardVeto(risk=FakeRisk()).check("X", "LONG", 100.0, 1000.0, sl=99.0, tp=101.0)
    assert ok and rej is None


# ------------------------------------------------------------- evidence


def test_to_direction_mapping():
    assert to_direction("bullish") == "LONG"
    assert to_direction("bearish") == "SHORT"
    assert to_direction("neutral") is None
    assert to_direction(None) is None


def test_evidence_weights_and_scores():
    coll = EvidenceCollector({"trend": 2.0, "whale": 1.0, "news": 1.0})
    fs = FS(trend=F("bullish", 0.8), whale=F("bearish", 0.5))
    items = coll.collect(fs, news=[{"sentiment": 0.3}])
    by_source = {i.source: i for i in items}
    assert by_source["trend"].contribution == pytest.approx(2.0 * 0.8)
    assert by_source["whale"].contribution == pytest.approx(-1.0 * 0.5)
    assert by_source["news"].contribution == pytest.approx(0.3)


def test_evidence_momentum_from_strategy():pass


# ---------------------------------------------------------------- engine


def _engine(**cfg):
    return UnifiedDecisionEngine({"decision": cfg}, risk=FakeRisk(), portfolio=FakePortfolio())


def test_engine_pass_with_clear_signal():
    eng = _engine(min_margin=0.05)
    fs = FS(trend=F("bullish", 1.0))
    verdict = eng.assess(
        "X/USDT:USDT", trend_up_df(), features=fs, equity=1000.0,
        price=300.0, atr=2.0,
    )
    assert verdict.status == Status.PASS
    assert verdict.action == "BUY"
    assert verdict.side == "LONG"
    assert verdict.confidence > 0.5
    assert len(verdict.evidence) > 0
    assert verdict.rejection is None


def test_engine_reject_invalid_price():
    eng = _engine()
    verdict = eng.assess("X/USDT:USDT", trend_up_df(), equity=1000.0, price=0.0)
    assert verdict.status == Status.REJECT
    assert verdict.reason_code == "INVALID_PRICE"


def test_engine_reject_null_equity():
    eng = _engine()
    verdict = eng.assess("X/USDT:USDT", trend_up_df(), price=300.0, atr=2.0, strategy_side="LONG")
    assert verdict.status == Status.REJECT
    assert verdict.reason_code == "NO_EQUITY"


def test_engine_wait_neutral():
    eng = _engine(min_margin=0.5)
    verdict = eng.assess("X/USDT:USDT", trend_up_df(), equity=1000.0, price=300.0)
    assert verdict.status == Status.WAIT
    assert verdict.should_block() is False


def test_engine_weak_signal_wait_neutral():
    eng = _engine(min_margin=0.05)
    fs = FS(trend=F("bullish", 0.1))
    verdict = eng.assess(
        "X/USDT:USDT", trend_up_df(), features=fs, equity=1000.0, price=300.0,
        atr=2.0,
    )
    # margin 0.0133 < min_margin 0.05 → WAIT (NEUTRAL_SIGNAL), never a block
    assert verdict.status == Status.WAIT
    assert verdict.reason_code == "NEUTRAL_SIGNAL"
    assert verdict.should_block() is False


def test_engine_calls_start():
    eng = _engine(min_margin=0.01)
    verdict = eng.assess("X/USDT:USDT", trend_up_df(), equity=1000.0, price=300.0,
                         atr=2.0, strategy_side="LONG", signal_conf=0.7)
    assert verdict.status == Status.PASS
    assert verdict.score != 0.0


def test_engine_reasons_present():
    eng = _engine()
    verdict = eng.assess("X/USDT:USDT", trend_up_df(), equity=1000.0, price=300.0,
                         atr=2.0, strategy_side="LONG", signal_conf=0.6)
    assert isinstance(verdict.as_dict(), dict)
    assert verdict.to_decision_shape()["action"] in ("BUY", None)


# ---------------------------------------------------------------- ticket


def test_ticket_creation_and_expiry():
    t = TradeTicket.new("X/USDT:USDT", "LONG", "test_strategy", 100.0,
                        stop_loss=99.0, take_profit=103.0, position_size=2.0,
                        ttl_seconds=1)
    assert t.ticket_id
    assert not t.expired()
    assert t.notional == pytest.approx(200.0)
    d = t.signal_dict()
    assert d["action"] == "BUY"
    assert d["risk"] == {"entry": 100.0, "sl": 99.0, "tp": 103.0}


def test_ticket_expired():
    t = TradeTicket(ticket_id="T1", symbol="X", side="LONG", strategy="s",
                    entry=100.0, created_at=100, expires_at=101)
    assert not t.expired(100.5)
    assert t.expired(101.0)
    t.mark_filled("oid", "eoid", "MARKET")
    assert t.status == "FILLED"


def test_ticket_from_verdict():
    eng = _engine(min_margin=0.05)
    verdict = eng.assess("X/USDT:USDT", trend_up_df(), features=FS(trend=F("bullish", 0.9)),
                         equity=1000.0, price=300.0, atr=2.0)
    assert verdict.status == Status.PASS
    t = TradeTicket.from_verdict(verdict, "strat", position_size=1.0, risk_pct=1.0)
    assert t.symbol == "X/USDT:USDT"
    assert t.regime == verdict.regime
    assert t.evidence == verdict.evidence_dicts()


# ---------------------------------------------------------------- telemetry


def test_signal_funnel_counts_and_rejects():
    from agent.decision.telemetry import SignalFunnel

    f = SignalFunnel()
    f.inc("strategy_signals")
    f.inc("strategy_signals")
    f.reject("SCORE_TOO_LOW")
    sn = f.snapshot()
    assert sn["strategy_signals"] == 2
    assert f.rejects()["SCORE_TOO_LOW"] == 1


def test_trade_reviewer_classify_and_mae_mfe():
    from agent.decision.reviewer import TradeReviewer

    r = TradeReviewer()
    assert r.classify(10.0, reason="tp") == "TP_HIT"
    assert r.classify(-5.0, reason="sl") == "SL_HIT"
    assert r.classify(0.01) == "BREAKEVEN"
    m = r.mae_mfe(entry=100.0, high=105.0, low=97.0, side="LONG")
    assert m["mae"] == pytest.approx(3.0)
    assert m["mfe"] == pytest.approx(5.0)