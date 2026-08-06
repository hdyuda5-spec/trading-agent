"""LLM advisory: structured JSON only, Decision Engine is the sole authority.

Guarantees:
- the LLM output is parsed as a strict structured advisory (trend +
  confidence + qualitative fields); any free text / stray action field is
  rejected or ignored — no natural-language parsing.
- the LLM never returns a BUY/SELL verdict; the final side always comes from
  the Decision Engine's weighted vote.
"""

import pytest

from agent.strategies.ai_signal import AISignalStrategy
from agent.strategies.decision import DecisionEngine

from tests.conftest import flat_df, uptrend_df
from tests.test_features.test_strategy_consumers import risk_cfg


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def info(self, *a):
        self.messages.append(a)

    def alert(self, *a):
        self.messages.append(a)


def make_strategy(**overrides):
    strat_cfg = {
        "model": "test",
        "confidence_threshold": 65,
        "prompt_template": "{}",
        "news_api_url": "",
        "json_mode": False,
    }
    strat_cfg.update(overrides)
    return AISignalStrategy(risk_cfg(), strat_cfg, None, FakeNotifier())


def make_fut(symbol="T/USDT:USDT", atr=1.0, rsi=60.0, df=None):
    df = df if df is not None else uptrend_df()
    strat = make_strategy()
    features = strat.decision.resolve_features(symbol, df, None)
    return {
        "df": df,
        "features": features,
        "price": float(df["close"].iloc[-1]),
        "rsi": rsi,
        "atr": atr,
    }


ADVISORY = {
    "market_summary": "ringkasan",
    "trend": "bullish",
    "macro": "makro",
    "news": "berita",
    "risk": "risiko",
    "confidence": 0.9,
}


# ------------------------------------------------------------ parsing


class TestParseAdvisory:
    def test_valid_structured_advisory(self):
        parsed = AISignalStrategy._parse_advisory(
            '{"market_summary":"s","trend":"bullish","macro":"m","news":"n",'
            '"risk":"r","confidence":0.81}'
        )
        assert parsed["trend"] == "bullish"
        assert parsed["confidence"] == pytest.approx(0.81)
        assert parsed["market_summary"] == "s"

    def test_confidence_accepts_0_100_and_0_1(self):
        assert AISignalStrategy._parse_advisory('{"trend":"bearish","confidence":81}')["confidence"] == pytest.approx(0.81)
        assert AISignalStrategy._parse_advisory('{"trend":"bearish","confidence":0.81}')["confidence"] == pytest.approx(0.81)

    def test_invalid_trend_rejected(self):
        assert AISignalStrategy._parse_advisory('{"trend":"buy","confidence":70}') is None
        assert AISignalStrategy._parse_advisory('{"trend":"LONG","confidence":70}') is None

    def test_missing_confidence_rejected(self):
        assert AISignalStrategy._parse_advisory('{"trend":"bullish"}') is None

    def test_non_structured_rejected(self):
        assert AISignalStrategy._parse_advisory("not json") is None
        assert AISignalStrategy._parse_advisory(None) is None
        assert AISignalStrategy._parse_advisory("[]") is None
        assert AISignalStrategy._parse_advisory('{"trend":"bullish","confidence":"high"}') is None

    def test_stray_action_field_ignored(self):
        # Even if the model sneaks a buy/sell in, only the advisory schema is read.
        parsed = AISignalStrategy._parse_advisory(
            '{"trend":"bullish","confidence":70,"action":"SELL","case":"buy"}'
        )
        assert parsed is not None
        assert "action" not in parsed
        assert "case" not in parsed
        assert parsed["trend"] == "bullish"


class TestNormalizeConfidence:
    def test_scale_handling(self):
        assert AISignalStrategy._normalize_confidence(81) == pytest.approx(0.81)
        assert AISignalStrategy._normalize_confidence(0.81) == pytest.approx(0.81)
        assert AISignalStrategy._normalize_confidence(150) == 1.0
        assert AISignalStrategy._normalize_confidence(-5) == 0.0

    def test_invalid(self):
        assert AISignalStrategy._normalize_confidence("abc") is None
        assert AISignalStrategy._normalize_confidence(None) is None


# ------------------------------------------------------- engine verdict


class TestDecisionEngineAdvisory:
    def test_advisory_adds_vote_and_shifts_margin(self):
        df = uptrend_df()
        base = DecisionEngine(risk_cfg()).decide("T", df)
        adv = {"trend": "bearish", "confidence": 0.9}
        tilted = DecisionEngine(risk_cfg()).decide("T", df, advisory=adv)
        assert tilted["advisory"] == {"trend": "bearish", "confidence": 0.9, "weight": 0.2}
        assert "ai_advisory" in [v["feature"] for v in tilted["votes"]]
        assert tilted["margin"] < base["margin"]
        assert -1.0 <= tilted["margin"] <= 1.0

    def test_advisory_strong_enough_flips_verdict(self):
        df = flat_df()
        base = DecisionEngine(risk_cfg()).decide("T", df)
        assert base["vote_side"] is None
        flipped = DecisionEngine(risk_cfg()).decide("T", df, advisory={"trend": "bullish", "confidence": 1.0})
        assert flipped["vote_side"] == "LONG"
        assert flipped["confidence"] > 0.5

    def test_neutral_advisory_no_vote(self):
        v = DecisionEngine(risk_cfg()).decide("T", uptrend_df(), advisory={"trend": "neutral", "confidence": 1.0})
        assert v["advisory"] is None
        assert "ai_advisory" not in [x["feature"] for x in v["votes"]]

    def test_advisory_weight_configurable(self):
        df = flat_df()
        v = DecisionEngine(risk_cfg(), weights={"ai_advisory": 0.5}).decide(
            "T", df, advisory={"trend": "bullish", "confidence": 1.0}
        )
        assert v["advisory"]["weight"] == 0.5


# -------------------------------------------------------- build signal


class TestBuildSignal:
    def test_side_comes_from_decision_engine(self):
        strat = make_strategy()
        fut = make_fut()
        sig = strat._build_signal("T/USDT:USDT", dict(ADVISORY), fut["rsi"], fut)
        assert sig is not None
        assert sig["action"] in ("BUY", "SELL")
        assert sig["side"] in ("LONG", "SHORT")
        assert sig["metadata"]["ai_trend"] == "bullish"
        assert any("ai_advisory" in r for r in sig["reason"])
        assert sig["risk"]["sl"] < sig["price"] < sig["risk"]["tp"]

    def test_below_threshold_advisory_counts_neutral(self):
        strat = make_strategy()
        fut = make_fut()
        advisory = dict(ADVISORY)
        advisory["confidence"] = 0.3  # < 65
        sig = strat._build_signal("T/USDT:USDT", advisory, fut["rsi"], fut)
        assert sig is not None
        assert sig["metadata"]["ai_trend"] == "neutral"
        assert not any("ai_advisory" in r for r in sig["reason"])

    def test_invalid_advisory_no_signal(self):
        strat = make_strategy()
        fut = make_fut()
        assert strat._build_signal("T/USDT:USDT", None, fut["rsi"], fut) is None

    def test_unrecognized_trend_ignored_by_engine(self):
        strat = make_strategy()
        fut = make_fut()
        bad = dict(ADVISORY)
        bad["trend"] = "buy"  # not a valid advisory trend -> no vote, no crash
        sig = strat._build_signal("T/USDT:USDT", bad, fut["rsi"], fut)
        assert not any("ai_advisory" in r for r in sig["reason"])
        assert sig["metadata"]["ai_trend"] == "buy"

    def test_no_atr_skips(self):
        strat = make_strategy()
        fut = make_fut()
        fut["atr"] = None
        assert strat._build_signal("T/USDT:USDT", dict(ADVISORY), fut["rsi"], fut) is None

    def test_debate_metadata_convictions(self):
        strat = make_strategy()
        fut = make_fut()
        fut["bull_arg"] = {"case": "b", "conviction": 70}
        fut["bear_arg"] = {"case": "s", "conviction": 40}
        sig = strat._build_signal("T/USDT:USDT", dict(ADVISORY), fut["rsi"], fut)
        assert sig["metadata"]["bull_conviction"] == 70
        assert sig["metadata"]["bear_conviction"] == 40
