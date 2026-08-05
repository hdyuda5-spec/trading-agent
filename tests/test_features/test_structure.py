"""StructureFeature: candlestick patterns + support/resistance levels."""

from agent.features import StructureFeature

from tests.conftest import flat_df, make_df, near_resistance_df, uptrend_df


def structure_feature(config=None):
    cfg = {"features": {"structure": {"sr_window": 3, "sr_cluster_pct": 0.5, "zone_pct": 0.6}}}
    return StructureFeature(None, cfg)


def test_neutral_with_levels():
    fr = structure_feature().compute("T/USDT:USDT", flat_df())
    assert fr.signal == "neutral"
    assert isinstance(fr.metadata["support"], list)
    assert isinstance(fr.metadata["resistance"], list)
    assert fr.metadata["sr_zone_pct"] == 0.6
    assert fr.metadata["pattern"] is None or isinstance(fr.metadata["pattern"], dict)


def test_uptrend_has_support_below():
    fr = structure_feature().compute("T/USDT:USDT", uptrend_df())
    price = fr.metadata["price"]
    assert all(s < price for s in fr.metadata["support"])


def test_near_resistance_bearish():
    df = near_resistance_df()
    fr = structure_feature().compute("T/USDT:USDT", df)
    assert fr.metadata["near_resistance"] is True
    assert fr.signal == "bearish"


def test_insufficient_data_neutral():
    fr = structure_feature().compute("T/USDT:USDT", make_df([100.0] * 4))
    assert fr.is_neutral


def test_df_only_flag():
    assert StructureFeature.df_only is True
