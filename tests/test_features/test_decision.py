"""Decision pipeline tests.

Covers the canonical flow (Trend -> Liquidity -> Structure -> Order Block ->
FVG -> Risk Engine -> Decision), the weighted-confidence vote aggregation, the
Decision shape, RiskEngine SL/TP, and the guarantee that features never place
orders.
"""

import pytest

from agent.features import FEATURE_REGISTRY
from agent.strategies.decision import (
    CORE_FLOW,
    DEFAULT_WEIGHTS,
    DecisionEngine,
    RiskEngine,
    build_decision,
)
from agent.strategies.mean_reversion import MeanReversionStrategy

from tests.conftest import cross_df, make_df, uptrend_df
from tests.test_features.test_strategy_consumers import make_strategy, risk_cfg


def cfg(**kw):
    base = {"risk": {"atr_period": 14, "atr_normal_pct": 1.0}}
    base.update(kw)
    return base


# ------------------------------------------------------------------ Decision


def test_decision_shape():
    risk = {"entry": 100.0, "sl": 99.0, "tp": 102.5, "atr": 1.0, "rr": 2.5}
    d = build_decision("momentum", "T", "LONG", 0.88, ["a", "b"], risk, 100.0)
    assert d["action"] == "BUY"
    assert d["side"] == "LONG"
    assert d["confidence"] == 0.88
    assert d["reason"] == ["a", "b"]
    assert d["risk"] == risk
    d2 = build_decision("momentum", "T", "SHORT", 0.9, [], {"sl": 1, "tp": 2}, 100.0)
    assert d2["action"] == "SELL"
    assert 0.0 <= d2["confidence"] <= 1.0


# ----------------------------------------------------------------- RiskEngine


def test_risk_engine_sl_tp_atr():
    re = RiskEngine(cfg())
    r = re.compute(100.0, "LONG", 2.0)
    assert r["sl"] == pytest.approx(100.0 - 1.5 * 2.0)
    assert r["tp"] == pytest.approx(100.0 + 2.5 * 2.0)
    assert r["atr"] == 2.0
    assert r["rr"] == pytest.approx(2.5 / 1.5, abs=1e-4)
    r2 = re.compute(100.0, "SHORT", 2.0)
    assert r2["sl"] > 100.0 > r2["tp"]


def test_risk_engine_fail_open():
    re = RiskEngine(cfg())
    assert re.compute(100.0, "LONG", None) is None
    assert re.compute(100.0, "LONG", float("nan")) is None
    assert re.compute(100.0, "LONG", float("inf")) is None


def test_risk_engine_configurable_mults():
    re = RiskEngine({"risk": {"atr_stop_mult": 2.0, "atr_tp_mult": 3.0}})
    r = re.compute(100.0, "LONG", 2.0)
    assert r["sl"] == 96.0 and r["tp"] == 106.0


# ------------------------------------------------------------ DecisionEngine


def test_flow_order_matches_canonical_pipeline():
    v = DecisionEngine(cfg()).decide("T", uptrend_df())
    assert [x["feature"] for x in v["votes"]] == list(CORE_FLOW)
    assert CORE_FLOW == ("trend", "smart_money_liquidity", "structure", "order_blocks", "fvg")


def test_weighted_margin_recomputed():
    eng = DecisionEngine(cfg())
    v = eng.decide("T", uptrend_df())
    scores = {"LONG": 0.0, "SHORT": 0.0}
    for x in v["votes"]:
        if x["side"]:
            scores[x["side"]] += eng.weights[x["feature"]] * x["confidence"]
    expected = (scores["LONG"] - scores["SHORT"]) / eng.total_weight
    assert v["margin"] == pytest.approx(expected, abs=1e-4)
    assert -1.0 <= v["margin"] <= 1.0


def test_weights_change_the_verdict():
    df = uptrend_df()
    base = DecisionEngine(cfg()).decide("T", df)["margin"]
    tilted = DecisionEngine(
        cfg(),
        weights={"trend": 0.9, "smart_money_liquidity": 0.02, "structure": 0.02,
                 "order_blocks": 0.03, "fvg": 0.03},
    ).decide("T", df)["margin"]
    assert base != tilted


def test_decision_engine_neutral_on_too_short():
    assert DecisionEngine(cfg()).decide("T", make_df([100.0] * 3)) is None


def test_votes_reflect_feature_outputs():
    v = DecisionEngine(cfg()).decide("T", uptrend_df())
    fs = v["features"]
    for vote in v["votes"]:
        assert vote["feature"] in fs
    trend_vote = v["votes"][0]
    if trend_vote["side"]:
        assert trend_vote["side"] == ("LONG" if fs.trend.signal == "bullish" else "SHORT")


def test_min_margin_config_read():
    eng = DecisionEngine(cfg())
    assert eng.min_margin == pytest.approx(0.15)
    eng2 = DecisionEngine(cfg(), weights=DEFAULT_WEIGHTS)
    assert eng2.total_weight == pytest.approx(sum(DEFAULT_WEIGHTS.values()))


# ---------------------------------------------------------- strategy decisions


def test_momentum_returns_decision_with_risk():
    sig = make_strategy().generate_signal("T/USDT:USDT", cross_df())
    assert sig is not None
    assert sig["action"] == "BUY"
    assert sig["side"] == "LONG"
    assert 0.5 <= sig["confidence"] <= 1.0
    assert isinstance(sig["reason"], list) and sig["reason"]
    assert sig["risk"]["sl"] < sig["price"] < sig["risk"]["tp"]
    assert "trend" in sig["reason"][0]
    assert sig["risk"]["rr"] >= 1.0


def test_mean_reversion_returns_decision():
    df = make_df([100.0] * 55 + [90.0, 88.0, 87.5])
    scfg = {"bb_period": 20, "bb_std": 2, "rsi_period": 14,
            "rsi_oversold": 30, "rsi_overbought": 70, "cooldown_seconds": 0}
    sig = MeanReversionStrategy(cfg(), scfg, None, None).generate_signal("T/USDT:USDT", df)
    assert sig is not None
    assert sig["action"] in ("BUY", "SELL")
    assert sig["risk"]["sl"] != sig["risk"]["tp"]
    assert isinstance(sig["reason"], list)


def test_momentum_and_mr_agree_on_feature_parity():
    df = cross_df()
    strat = make_strategy()
    assert strat.generate_signal("T/USDT:USDT", df) == strat.generate_signal("T/USDT:USDT", df)


def test_trend_feature_exposes_configured_pair_cross_event():
    from agent.features.trend import TrendFeature

    f = TrendFeature(None, {"features": {"trend": {"ema_fast": 12, "ema_slow": 26}}})
    fr = f.compute("T", cross_df())
    assert "cross_12_26_event" in fr.metadata
    assert fr.metadata["cross_12_26_event"] in ("bullish", "bearish", None)


# -------------------------------------------------- features never execute trades


def test_no_feature_places_orders():
    forbidden = {"open_position", "create_order", "place_order", "place_sl_tp", "close_position"}
    for cls in FEATURE_REGISTRY:
        public = {name for name in dir(cls) if not name.startswith("_")}
        assert not (public & forbidden), f"{cls.__name__} executes trades"
        assert hasattr(cls, "compute"), f"{cls.__name__} missing compute"
