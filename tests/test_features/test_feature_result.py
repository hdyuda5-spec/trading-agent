"""FeatureResult contract: immutability, clamping, serialization shape."""

import pytest

from agent.features import FeatureResult


def test_to_dict_schema():
    fr = FeatureResult("trend", "bullish", 0.83, {"ema9": 1.0})
    assert fr.to_dict() == {"trend": "bullish", "confidence": 0.83, "metadata": {"ema9": 1.0}}


def test_as_mapping_schema():
    fr = FeatureResult("trend", "bearish", 0.7, {})
    assert fr.as_mapping() == {"signal": "bearish", "confidence": 0.7, "metadata": {}}


def test_confidence_clamped_and_rounded():
    assert FeatureResult("x", "bullish", 5.0).confidence == 1.0
    assert FeatureResult("x", "bullish", -3.0).confidence == 0.0
    assert FeatureResult("x", "bullish", 0.123456).confidence == 0.1235


def test_is_neutral():
    assert FeatureResult.neutral("volatility").is_neutral
    assert not FeatureResult("volatility", "high", 0.9).is_neutral


def test_metadata_immutable():
    fr = FeatureResult("trend", "bullish", 0.5, {"k": 1})
    with pytest.raises(TypeError):
        fr.metadata["k"] = 2


def test_dataclass_immutable():
    fr = FeatureResult("trend", "bullish", 0.5)
    with pytest.raises(Exception):
        fr.signal = "bearish"
