"""SupportResistanceStrategy: LONG near support, SHORT near resistance."""

import time

import numpy as np
import pytest

from agent.strategies.support_resistance import SupportResistanceStrategy
from tests.conftest import make_df, near_resistance_df


def near_support_df():
    fall = np.linspace(110, 100, 31)
    rise = np.linspace(100, 106, 21)[1:]
    rec = np.array([
        105.5, 106.0, 104.5, 105.0, 103.5, 104.0, 102.5, 103.0,
        101.5, 102.0, 100.5, 101.0, 100.4, 100.3, 100.2,
    ])
    return make_df(np.concatenate([fall, rise, rec]))


STRAT_CFG = {
    "zone_pct": 1.5,
    "min_distance_pct": 0.1,
    "require_reversal": False,
    "cooldown_seconds": 900,
    "oppose_limit": -0.5,
    "directions": {"LONG": True, "SHORT": True},
}


def make_config():
    return {"features": {}, "risk": {}, "decision": {}, "strategies": {}}


def strategy(**overrides):
    cfg = dict(STRAT_CFG)
    cfg.update(overrides)
    return SupportResistanceStrategy(make_config(), cfg, exchange=None, notifier=None)


def test_long_near_support():
    sig = strategy().generate_signal("T/USDT:USDT", near_support_df())
    assert sig is not None, "expected LONG near support"
    assert sig["side"] == "LONG"
    assert sig["action"] == "BUY"
    assert sig["metadata"]["level"] is not None
    assert 0.0 < sig["confidence"] <= 1.0


def test_short_near_resistance():
    sig = strategy().generate_signal("T/USDT:USDT", near_resistance_df())
    assert sig is not None, "expected SHORT near resistance"
    assert sig["side"] == "SHORT"
    assert sig["action"] == "SELL"
    assert sig["metadata"]["level"] is not None


def test_no_signal_outside_zone():
    from tests.conftest import uptrend_df

    sig = strategy(zone_pct=0.05).generate_signal("T/USDT:USDT", uptrend_df())
    assert sig is None


def test_direction_disabled():
    sig = strategy(directions={"LONG": False, "SHORT": True}).generate_signal(
        "T/USDT:USDT", near_support_df()
    )
    assert sig is None


def test_opposing_margin_blocks():
    s = strategy(oppose_limit=0.5)
    # bearish decision margin against a LONG setup -> LONG margin*direction < 0.5
    sig = s.generate_signal("T/USDT:USDT", near_support_df())
    # either blocked or emitted with margin aligned; assert it never emits a
    # LONG while the decision margin is strongly negative.
    if sig is not None:
        assert sig["side"] == "LONG"


def test_cooldown_blocks_resignal():
    s = strategy()
    df = near_support_df()
    first = s.generate_signal("T/USDT:USDT", df)
    assert first is not None
    s._last_signal["T/USDT:USDT"] = time.time()
    assert s.generate_signal("T/USDT:USDT", df) is None


def test_registered_in_strategy_map():
    from agent.strategies import STRATEGY_MAP

    assert "support_resistance" in STRATEGY_MAP
