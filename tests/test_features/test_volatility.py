"""VolatilityFeature: ATR/Bollinger classification and metadata."""

from agent.features import VolatilityFeature

from tests.conftest import choppy_df, flat_df, make_df


def vol_feature(config=None):
    return VolatilityFeature(None, {"risk": {"atr_period": 14, "atr_normal_pct": 1.0}})


def test_choppy_df_high():
    fr = vol_feature().compute("T/USDT:USDT", choppy_df())
    assert fr.signal == "high"
    assert fr.confidence >= 0.5
    assert fr.metadata["atr"] > 0
    assert fr.metadata["atr_pct"] > 1.0


def test_flat_df_low():
    fr = vol_feature().compute("T/USDT:USDT", flat_df())
    assert fr.signal == "low"


def test_bollinger_metadata():
    fr = vol_feature().compute("T/USDT:USDT", flat_df())
    for key in ("bb_period", "bb_std", "bb_sma", "bb_upper", "bb_lower", "bb_width_pct"):
        assert key in fr.metadata
    assert fr.metadata["bb_period"] == 20
    assert fr.metadata["bb_std"] == 2.0
    assert fr.metadata["bb_upper"] >= fr.metadata["bb_sma"] >= fr.metadata["bb_lower"]


def test_custom_bb_config():
    cfg = {"risk": {"atr_period": 14, "atr_normal_pct": 1.0},
           "features": {"volatility": {"bb_period": 50, "bb_std": 1.5}}}
    fr = VolatilityFeature(None, cfg).compute("T/USDT:USDT", make_df([100.0] * 120))
    assert fr.metadata["bb_period"] == 50
    assert fr.metadata["bb_std"] == 1.5


def test_insufficient_data_neutral():
    fr = vol_feature().compute("T/USDT:USDT", make_df([100.0] * 3))
    assert fr.is_neutral
