def _open(df, i):
    return float(df["open"].iloc[i])


def _close(df, i):
    return float(df["close"].iloc[i])


def _high(df, i):
    return float(df["high"].iloc[i])


def _low(df, i):
    return float(df["low"].iloc[i])


def _body(df, i):
    return abs(_close(df, i) - _open(df, i))


def _rng(df, i):
    return _high(df, i) - _low(df, i)


def _is_bull(df, i):
    return _close(df, i) > _open(df, i)


def _is_bear(df, i):
    return _close(df, i) < _open(df, i)


def _small_body(df, i):
    return _body(df, i) <= 0.1 * (_rng(df, i) or 1)


def _swing_points(df, n, window=3):
    highs = []
    lows = []
    for j in range(window, n - window):
        h = _high(df, j)
        l = _low(df, j)
        if all(h > _high(df, k) for k in range(j - window, j + window + 1) if k != j):
            highs.append((j, h))
        if all(l < _low(df, k) for k in range(j - window, j + window + 1) if k != j):
            lows.append((j, l))
    return highs, lows


def _double_pattern(df, sw, tol=0.02):
    highs, lows = sw
    highs = highs[-6:]
    lows = lows[-6:]
    i = len(df) - 1
    if len(highs) >= 2:
        (ia, ha), (ib, hb) = highs[-2], highs[-1]
        if ib > ia and abs(ha - hb) / max(ha, hb) <= tol:
            neck = min(_low(df, k) for k in range(ia, ib + 1))
            if _close(df, i) < neck:
                return ("Double Top", "bearish")
    if len(lows) >= 2:
        (ia, la), (ib, lb) = lows[-2], lows[-1]
        if ib > ia and abs(la - lb) / max(la, lb) <= tol:
            neck = max(_high(df, k) for k in range(ia, ib + 1))
            if _close(df, i) > neck:
                return ("Double Bottom", "bullish")
    return None


def detect_patterns(df):
    n = len(df)
    if n < 5:
        return None
    i = n - 1
    found = []

    if _is_bull(df, i) and _is_bear(df, i - 1):
        if _open(df, i) <= _close(df, i - 1) and _close(df, i) >= _open(df, i - 1):
            found.append(("Bullish Engulfing", "bullish"))
    if _is_bear(df, i) and _is_bull(df, i - 1):
        if _open(df, i) >= _close(df, i - 1) and _close(df, i) <= _open(df, i - 1):
            found.append(("Bearish Engulfing", "bearish"))

    if _small_body(df, i):
        found.append(("Doji", "neutral"))

    rng = _rng(df, i)
    if rng > 0:
        body = _body(df, i)
        upper = _high(df, i) - max(_open(df, i), _close(df, i))
        lower = min(_open(df, i), _close(df, i)) - _low(df, i)
        if lower >= 2 * body and upper <= 0.3 * body:
            found.append(("Hammer", "bullish"))
        if upper >= 2 * body and lower <= 0.3 * body:
            found.append(("Shooting Star", "bearish"))

    if n >= 3:
        if _is_bear(df, i - 2) and _small_body(df, i - 1) and _is_bull(df, i):
            if _close(df, i) > _open(df, i - 2) and _close(df, i - 1) < _close(df, i - 2):
                found.append(("Morning Star", "bullish"))
        if _is_bull(df, i - 2) and _small_body(df, i - 1) and _is_bear(df, i):
            if _close(df, i) < _open(df, i - 2) and _close(df, i - 1) > _close(df, i - 2):
                found.append(("Evening Star", "bearish"))

    if n >= 2 and _is_bear(df, i - 1) and _is_bull(df, i):
        mid = (_open(df, i - 1) + _close(df, i - 1)) / 2
        if _open(df, i) < _low(df, i - 1) and _close(df, i) > mid:
            found.append(("Piercing Line", "bullish"))
    if n >= 2 and _is_bull(df, i - 1) and _is_bear(df, i):
        mid = (_open(df, i - 1) + _close(df, i - 1)) / 2
        if _open(df, i) > _high(df, i - 1) and _close(df, i) < mid:
            found.append(("Dark Cloud Cover", "bearish"))

    if n >= 4:
        if all(_is_bull(df, j) and _close(df, j) > _close(df, j - 1) for j in (i, i - 1, i - 2)):
            found.append(("Three White Soldiers", "bullish"))
        if all(_is_bear(df, j) and _close(df, j) < _close(df, j - 1) for j in (i, i - 1, i - 2)):
            found.append(("Three Black Crows", "bearish"))

    dbl = _double_pattern(df, _swing_points(df, n))
    if dbl:
        found.append(dbl)

    if found:
        priority = {"bullish": 2, "bearish": 2, "neutral": 1}
        found.sort(key=lambda p: (priority.get(p[1], 1), len(p[0])), reverse=True)
        return {"name": found[0][0], "direction": found[0][1]}
    return None
