"""Risk Engine V2 — risk-per-trade sizing + RR validation (Phases 8-9)."""

import pytest

from agent.core.risk import RiskEngine


class FakeClient:
    def __init__(self, markets=None):
        self.markets = markets or {
            "X/USDT:USDT": {
                "limits": {"cost": {"min": 5.0}, "amount": {"min": 0.01}},
                "precision": {"amount": 3},
                "precisionMode": 1,
                "info": {"filters": []},
            }
        }

    def amount_to_precision(self, symbol, qty):
        return round(qty, 6)


class FakeExchange:
    def __init__(self, client=None):
        self.client = client or FakeClient()

    def fetch_order_book(self, symbol, limit=5):
        return {"bids": [[100.0, 5.0]], "asks": [[100.1, 5.0]]}

    def fetch_open_orders(self):
        return []


def make_risk(**overrides):
    cfg = {
        "risk_per_trade_pct": 1.0,
        "max_position_pct": 10,
        "max_total_exposure_pct": 30,
        "leverage": 5,
        "max_leverage": 5,
        "daily_loss_limit_pct": 5.0,
        "min_equity_usdt": 20,
        "max_open_positions": 2,
        "min_fee_tolerance_pct": 0.15,
        "atr_stop_mult": 1.5,
        "atr_tp_mult": 2.5,
        "min_rr": 1.5,
    }
    cfg.update(overrides)
    return RiskEngine(cfg, FakeExchange())


# -- Risk-per-trade sizing ---------------------------------------------


def test_size_uses_risk_amount_over_sl_distance():
    r = make_risk(risk_per_trade_pct=1.0)
    # entry=100, sl=99 → dist=1; equity=1000 → risk_amount=10 → qty=10 → notional=1000
    # capped by max_position_pct 10% of 1000 = 100 → qty=1.0
    qty = r.risk_position_size("X/USDT:USDT", 100.0, "buy", atr=0.5, equity=1000.0, stop_loss=99.0)
    assert qty == pytest.approx(1.0)


def test_tighter_stop_yields_larger_size_under_cap():
    r = make_risk(risk_per_trade_pct=1.0, max_position_pct=10)
    # dist=0.5 → qty=20 → notional=2000 → still capped at 100 → qty 1.0
    qty = r.risk_position_size("X/USDT:USDT", 100.0, "buy", atr=0.5, equity=1000.0, stop_loss=99.5)
    assert qty == pytest.approx(1.0)


def test_size_capped_by_max_position_pct():
    r = make_risk(risk_per_trade_pct=1.0, max_position_pct=5)
    qty = r.risk_position_size("X/USDT:USDT", 100.0, "buy", atr=0.5, equity=1000.0, stop_loss=95.0)
    # notional from risk = 10/5*100 = 200 → cap = 50 → 0.5
    assert qty == pytest.approx(0.5)


def test_size_zero_when_equity_missing_or_zero():
    r = make_risk()
    assert r.risk_position_size("X/USDT:USDT", 100.0, "buy", atr=0.5, stop_loss=99.0) == 0.0
    assert r.risk_position_size("X/USDT:USDT", 100.0, "buy", atr=0.5, equity=0.0, stop_loss=99.0) == 0.0


def test_size_zero_when_stop_equals_entry():
    r = make_risk()
    assert r.risk_position_size("X/USDT:USDT", 100.0, "buy", atr=0.5, equity=1000.0, stop_loss=100.0) == 0.0


def test_size_falls_back_to_atr_stop():
    r = make_risk(atr_stop_mult=1.5)
    qty = r.risk_position_size("X/USDT:USDT", 100.0, "buy", atr=2.0, equity=1000.0)
    # sl = 100 - 3 = 97, dist = 3, notional = 10/3*100 = 333 (cap 100) → 100/100 = 1.0
    assert qty == pytest.approx(1.0)


# -- RR validation ------------------------------------------------------


def test_rr_valid_long():
    r = make_risk(min_rr=1.5)
    ok, code, value = r.validate_rr(100.0, 99.0, 103.0, "buy")  # risk1 reward3 rr3
    assert ok and code == "OK"
    assert value == pytest.approx(3.0)


def test_rr_too_low_rejects_with_code():
    r = make_risk(min_rr=1.5)
    ok, code, _ = r.validate_rr(100.0, 99.0, 100.5, "buy")  # rr 0.5
    assert not ok
    assert code == "RR_TOO_LOW"


def test_rr_valid_short():
    r = make_risk(min_rr=1.5)
    ok, code, value = r.validate_rr(100.0, 102.0, 97.0, "sell")  # risk2 reward3 rr1.5
    assert ok and code == "OK"
    assert value == pytest.approx(1.5)


def test_rr_invalid_sl_zero_risk():
    r = make_risk()
    ok, code, _ = r.validate_rr(100.0, 100.0, 105.0, "buy")
    assert not ok
    assert code == "INVALID_SL"


def test_rr_invalid_levels():
    r = make_risk()
    ok, code, _ = r.validate_rr(0.0, 99.0, 105.0, "buy")
    assert not ok and code == "INVALID_LEVEL"


def test_min_rr_zero_disables_gate():
    r = make_risk(min_rr=0.0)
    ok, code, _ = r.validate_rr(100.0, 99.0, 100.5, "buy")
    assert ok and code == "OK"


def test_risk_rr_importable():
    from agent.core.risk import RiskBasedSizingPolicy, RRValidationPolicy  # noqa: F401