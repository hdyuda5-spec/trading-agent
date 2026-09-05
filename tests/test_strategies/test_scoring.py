"""ScreenerScorer + ScreeningGate: weighted confidence decision engine.

Covers the five hard-reject conditions, the factor weight table, the
confidence-margin thresholds, and the canonical ``{action, confidence,
reason[]}`` decision shape.
"""

import pytest

from agent.strategies.scoring import (
    WEIGHTS,
    ScreenerScorer,
    ScreeningGate,
    classify_order_reason,
)
from tests.conftest import make_df, uptrend_df, downtrend_df

FACTORS = [
    "liquidity",
    "structure",
    "trend",
    "volume",
    "funding",
    "open_interest",
    "whale",
]


def make_config(**overrides):
    cfg = {
        "features": {},
        "screener": {
            "timeframe": "1h",
            "rsi_confirmation": False,
            "max_extended_pct": 30,
            "scoring": {
                "min_confidence": 0.60,
                "min_margin": 0.10,
                "weights": {
                    "liquidity": 0.25,
                    "structure": 0.20,
                    "trend": 0.15,
                    "volume": 0.15,
                    "funding": 0.10,
                    "open_interest": 0.10,
                    "whale": 0.05,
                },
            },
        },
    }
    cfg["screener"]["scoring"].update(overrides)
    return cfg


def scorer(**scoring):
    return ScreenerScorer(make_config(**scoring), exchange=None)


class FakeExecution:
    def screen_trade_ok(self, *a, **k):
        return True, 100.0, None


class FakeRisk:
    def daily_loss_exceeded(self, equity):
        return False


class FakePortfolio:
    halted = False


class FakeExchange:
    def check_health(self):
        return True


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0, abs=1e-6)


def test_weights_match_spec():
    assert WEIGHTS["liquidity"] == 0.25
    assert WEIGHTS["structure"] == 0.20
    assert WEIGHTS["trend"] == 0.15
    assert WEIGHTS["volume"] == 0.15
    assert WEIGHTS["funding"] == 0.10
    assert WEIGHTS["open_interest"] == 0.10
    assert WEIGHTS["whale"] == 0.05


def test_uptrend_bullish_decision():
    v = scorer().evaluate("T/USDT:USDT", uptrend_df())
    assert v["action"] == "BUY"
    assert v["side"] == "LONG"
    assert v["confidence"] >= 0.60
    assert isinstance(v["reason"], list)
    assert v["margin"] >= 0.10
    assert not v["rejected"]


def test_downtrend_bearish_decision():
    v = scorer().evaluate("T/USDT:USDT", downtrend_df())
    assert v["action"] == "SELL"
    assert v["side"] == "SHORT"
    assert v["confidence"] >= 0.60


def test_decision_shape():
    v = scorer().evaluate("T/USDT:USDT", uptrend_df())
    d = ScreenerScorer.to_decision(v)
    assert set(d) == {"action", "confidence", "reason"}
    assert isinstance(d["action"], str)
    assert 0.0 <= d["confidence"] <= 1.0
    assert isinstance(d["reason"], list)


def test_confidence_bounded():
    v = scorer().evaluate("T/USDT:USDT", uptrend_df())
    assert 0.05 <= v["confidence"] <= 0.97


def test_flat_margin_rejected():
    v = scorer().evaluate("T/USDT:USDT", make_df([100.0] * 120))
    assert v["rejected"]
    assert "margin" in v["reject_reason"]


def test_low_confidence_rejected():
    s = scorer(min_confidence=0.99)
    v = s.evaluate("T/USDT:USDT", uptrend_df())
    assert v["rejected"]
    assert "confidence" in v["reject_reason"]


def test_high_min_margin_rejected():
    s = scorer(min_margin=0.9)
    v = s.evaluate("T/USDT:USDT", uptrend_df())
    assert v["rejected"]


def test_news_modifier_bends_confidence():
    s = scorer()
    up = uptrend_df()
    good = s.evaluate("T/USDT:USDT", up, news=[{"sentiment": 1.0}])
    bad = s.evaluate("T/USDT:USDT", up, news=[{"sentiment": -1.0}])
    assert good["confidence"] > bad["confidence"]


def test_news_negative_can_reject():
    s = scorer(min_confidence=0.97)
    v = s.evaluate("T/USDT:USDT", uptrend_df(), news=[{"sentiment": -1.0}])
    assert v["rejected"]


def test_extreme_move_penalty():
    s = scorer()
    up = uptrend_df()
    normal = s.evaluate("T/USDT:USDT", up, chg=1.0)
    extreme = s.evaluate("T/USDT:USDT", up, chg=60.0)
    assert extreme["confidence"] < normal["confidence"]


def test_losing_streak_penalty():
    s = scorer()
    up = uptrend_df()
    clean = s.evaluate("T/USDT:USDT", up)
    streak = s.evaluate("T/USDT:USDT", up, losing_streak=True)
    assert streak["confidence"] < clean["confidence"]


def test_regime_penalty():
    cfg = make_config()
    cfg["whale"] = {"market_regime": {"enabled": True, "net_sell_threshold_usdt": 100, "min_symbols": 1}}
    s = ScreenerScorer(cfg, None)
    up = uptrend_df()
    clean = s.evaluate("T/USDT:USDT", up, market_regime=(-50, 1))
    regime = s.evaluate("T/USDT:USDT", up, market_regime=(-500, 1))
    assert regime["confidence"] < clean["confidence"]


def test_all_factors_present():
    v = scorer().evaluate("T/USDT:USDT", uptrend_df())
    assert set(v["factors"]) == set(FACTORS)
    for val in v["factors"].values():
        assert -1.0 <= val <= 1.0


def test_rsi_in_range_bonus():
    cfg = make_config()
    cfg["screener"]["rsi_confirmation"] = True
    cfg["screener"]["rsi_long_range"] = [40, 75]
    s = ScreenerScorer(cfg, None)
    v = s.evaluate("T/USDT:USDT", uptrend_df())
    assert "rsi" in v["modifiers"]
    assert -0.12 <= v["modifiers"]["rsi"] <= 0.03


def test_rsi_confirmation_off_no_modifier():
    cfg = make_config()
    cfg["screener"]["rsi_confirmation"] = False
    s = ScreenerScorer(cfg, None)
    v = s.evaluate("T/USDT:USDT", uptrend_df())
    assert v["modifiers"]["rsi"] == 0.0


# ---- gate -------------------------------------------------------------

def gate():
    return ScreeningGate(make_config(), FakeRisk(), FakeExecution(), FakeExchange(), FakePortfolio())


def test_gate_exchange_available():
    assert gate().exchange_available() is True


def test_gate_exchange_down():
    class Down(FakeExchange):
        def check_health(self):
            return False

    g = ScreeningGate(make_config(), FakeRisk(), FakeExecution(), Down(), FakePortfolio())
    assert g.exchange_available() is False


def test_gate_check_global_ok():
    ok, category, detail = gate().check_global(100.0)
    assert ok is True
    assert category is None


def test_gate_check_global_low_equity():
    cfg = make_config()
    cfg["risk"] = {"min_equity_usdt": 20}
    g = ScreeningGate(cfg, FakeRisk(), FakeExecution(), FakeExchange(), FakePortfolio())
    ok, category, detail = g.check_global(1.0)
    assert not ok
    assert category == "insufficient_balance"


def test_gate_check_global_drawdown():
    class Halted(FakePortfolio):
        halted = True

    g = ScreeningGate(make_config(), FakeRisk(), FakeExecution(), FakeExchange(), Halted())
    ok, category, detail = g.check_global(100.0)
    assert not ok
    assert category == "max_drawdown"


def test_gate_check_order_ok():
    ok, category, detail, equity = gate().check_order("T/USDT:USDT", "LONG", 100.0, 1.0, 100.0, [])
    assert ok is True
    assert equity == 100.0


def test_gate_check_order_invalid():
    class Bad(FakeExecution):
        def screen_trade_ok(self, *a, **k):
            return False, None, "spread/fee terlalu tinggi"

    g = ScreeningGate(make_config(), FakeRisk(), Bad(), FakeExchange(), FakePortfolio())
    ok, category, detail, equity = g.check_order("T/USDT:USDT", "LONG", 100.0, 1.0, 100.0, [])
    assert not ok
    assert category == "invalid_order"


def test_gate_check_order_risk_violation():
    class Bad(FakeExecution):
        def screen_trade_ok(self, *a, **k):
            return False, None, "max total exposure reached"

    g = ScreeningGate(make_config(), FakeRisk(), Bad(), FakeExchange(), FakePortfolio())
    ok, category, detail, equity = g.check_order("T/USDT:USDT", "LONG", 100.0, 1.0, 100.0, [])
    assert not ok
    assert category == "risk_violation"


def test_classify_order_reason_budget():
    assert classify_order_reason("budget 4.20 < minCost 5") == "insufficient_balance"


def test_classify_order_reason_unknown():
    assert classify_order_reason("bomb exploded") == "invalid_order"
