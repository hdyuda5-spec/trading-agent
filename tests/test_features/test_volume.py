"""VolumeFeature: ratio classification + OBV trend."""

from agent.features import VolumeFeature

from tests.conftest import make_df, uptrend_df


def volume_feature(config=None):
    return VolumeFeature(None, config or {})


def test_rising_volume_spike():
    close = [100 + i * 0.2 for i in range(60)]
    volume = [100.0] * 58 + [1000.0, 50.0]
    fr = volume_feature().compute("T/USDT:USDT", make_df(close, volume))
    assert fr.signal == "rising"
    assert fr.metadata["vol_ratio"] > 1.2


def test_falling_volume():
    close = [100.0] * 60
    volume = [100.0] * 58 + [5.0, 50.0]
    fr = volume_feature().compute("T/USDT:USDT", make_df(close, volume))
    assert fr.signal == "falling"
    assert fr.metadata["vol_ratio"] < 0.8


def test_obv_rising_in_uptrend():
    fr = volume_feature().compute("T/USDT:USDT", uptrend_df())
    assert fr.metadata["obv_trend"] == "rising"


def test_neutral_flat():
    fr = volume_feature().compute("T/USDT:USDT", make_df([100.0] * 60))
    assert fr.signal == "neutral"
    assert fr.metadata["vol_ratio"] < 1.2


def test_metadata_contract():
    fr = volume_feature().compute("T/USDT:USDT", uptrend_df())
    assert {"volume", "vol_ratio", "obv_trend", "ratio_window"} <= set(fr.metadata.keys())


def test_insufficient_data_neutral():
    fr = volume_feature().compute("T/USDT:USDT", make_df([100.0] * 5))
    assert fr.is_neutral
