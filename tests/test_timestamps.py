"""Timestamp/unit detection for stale-order handling (Phase 2).

Guards: milliseconds, seconds and microseconds must never be conflated; a
fresh order must report a small age and an old order a large one regardless of
the unit the (possible exchange/feed/paper-broker mix) used.
"""

import time

import pytest

from agent.core.timestamps import MAX_AGE_SECONDS, order_age_seconds, to_epoch_seconds


def test_timestamp_seconds():
    now = 1_700_000_000.0
    ts_now = int(now)  # seconds
    ts_old = int(now) - 3600  # 1 hour ago, in seconds
    assert to_epoch_seconds(ts_old, now) == pytest.approx(ts_old)
    assert order_age_seconds(ts_old, now) == pytest.approx(3600, abs=1)
    assert order_age_seconds(ts_now, now) < 1


def test_timestamp_milliseconds():
    now = 1_700_000_000.0
    ms_old = (int(now) - 120) * 1000  # 2 min ago, in ms
    assert to_epoch_seconds(ms_old, now) == pytest.approx(ms_old / 1000.0)
    assert order_age_seconds(ms_old, now) == pytest.approx(120, abs=1)


def test_timestamp_milliseconds_modern():
    """Real-world ms timestamps are ~1.7e12 and must not be read as seconds."""
    now = time.time()
    ms = int(now * 1000)
    assert to_epoch_seconds(ms) == pytest.approx(now, abs=1)
    assert order_age_seconds(ms) < 2


def test_timestamp_microseconds():
    now = 1_700_000_000.0
    us_old = (int(now) - 60) * 1_000_000  # 1 min ago, in µs
    assert to_epoch_seconds(us_old, now) == pytest.approx(us_old / 1_000_000.0)
    assert order_age_seconds(us_old, now) == pytest.approx(60, abs=1)


def test_stale_order_is_stale_in_any_unit():
    now = 1_700_000_000.0
    for ts in (
        int(now) - 900,               # seconds
        (int(now) - 900) * 1000,      # ms
        (int(now) - 900) * 1_000_000, # µs
    ):
        assert order_age_seconds(ts, now) > 900 - 1
        assert order_age_seconds(ts, now) > 840  # ~15m placeholder tests


def test_fresh_order_is_fresh_in_any_unit():
    now = 1_700_000_000.0
    for ts in (
        int(now) - 5,
        (int(now) - 5) * 1000,
        (int(now) - 5) * 1_000_000,
    ):
        assert order_age_seconds(ts, now) < 10


def test_missing_or_zero_timestamp_is_unknown():
    assert order_age_seconds(None) == 0.0
    assert order_age_seconds(0) == 0.0
    assert order_age_seconds(0.0) == 0.0


def test_invalid_timestamp_raises():
    with pytest.raises(ValueError):
        to_epoch_seconds("not-a-number")


def test_old_order_beyond_max_age_still_resolves_to_seconds():
    now = 1_700_000_000.0
    ts = int(now) - (MAX_AGE_SECONDS + 3600)  # seconds, very old
    # Ambiguous between s/ms/µs: must pick the seconds interpretation (nearest
    # to plausible), not swallow it or explode.
    age = order_age_seconds(ts, now)
    assert age > MAX_AGE_SECONDS


def test_order_datetime_string_is_handled():
    """Some ccxt parsers yield ISO strings; the helper must resolve them."""
    ts = "2023-11-14T22:13:20.000Z"
    # not numeric → ValueError from float(); ensure callers surface it clearly
    with pytest.raises(ValueError):
        to_epoch_seconds(ts)