"""Timestamp helpers for order and exchange time handling.

ccxt order objects carry ``timestamp`` in **milliseconds**. Some exchanges,
feeds and simulated (paper) brokers return **seconds** or even **microseconds**
or mix units within the same code path. A wrong guess silently turns a
minutes-old order into hours-old and gets it cancelled (or never cancelled).

Strategy: convert a timestamp to epoch-seconds by trying each plausible unit
(seconds, milliseconds, microseconds) and accepting the first conversion whose
derived ``age = now - epoch`` falls in ``[0, MAX_AGE_SECONDS]`` (an order cannot
be older than this nor in the future). If none matches, fall back to the unit
whose age is closest to zero. The result is deterministic and unit-agnostic.
"""

import time

# Orders older than this are treated as legacy/unfixable; used only to pick
# the correct unit, never as the stale threshold itself.
MAX_AGE_SECONDS = 90 * 24 * 3600  # 90 days

_MS = 1000.0
_US = 1_000_000.0
_UNITS = (1.0, _MS, _US)


def to_epoch_seconds(timestamp, now=None) -> float:
    """Best-effort conversion of an ms/s/µs timestamp to epoch seconds.

    Returns ``0.0`` for missing/zero/invalid input (callers must treat 0 as
    "unknown" and skip). Raises ``ValueError`` on non-numeric input.
    """
    if timestamp is None:
        return 0.0
    now = now if now is not None else time.time()
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        raise ValueError(f"non-numeric timestamp: {timestamp!r}")
    if ts <= 0:
        return 0.0
    best = None
    for unit in _UNITS:
        epoch = ts / unit
        age = now - epoch
        if 0 <= age <= MAX_AGE_SECONDS:
            return epoch  # unambiguous
        if best is None or abs(age) < abs(best[0]):
            best = (age, epoch)
    return best[1]


def order_age_seconds(timestamp, now=None) -> float:
    """Age of an order in seconds, regardless of the original unit.

    Returns ``0.0`` for missing/unknown timestamps so ``age > ttl`` stays
    False and an order with no timestamp is never spuriously cancelled.
    """
    now = now if now is not None else time.time()
    epoch = to_epoch_seconds(timestamp, now)
    if epoch <= 0:
        return 0.0
    age = now - epoch
    return max(0.0, age)