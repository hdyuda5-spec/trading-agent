"""RiskEngine: policy composition, injection and backward compatibility."""

import math

import pytest

from agent.core.risk import (
    DrawdownPolicy,
    ExposurePolicy,
    LeveragePolicy,
    PositionSizingPolicy,
    RiskEngine,
    RiskManager,
    StopLossPolicy,
    TakeProfitPolicy,
    ValidationPolicy,
)


class FakeExchange:
    def __init__(self, balance=None, open_orders=None, book=None):
        self.balance = balance or {"USDT": {"total": 1000.0, "free": 900.0}}
        self.open_orders = open_orders or []
        self.book = book or {"bids": [[100.0, 5.0]], "asks": [[100.1, 5.0]]}
        self.calls = {"margin_mode": [], "leverage": [], "order_book": 0, "balance": 0, "open_orders": 0}

    def fetch_balance(self):
        self.calls["balance"] += 1
        return self.balance

    def fetch_open_orders(self):
        self.calls["open_orders"] += 1
        return self.open_orders

    def fetch_order_book(self, symbol, limit=5):
        self.calls["order_book"] += 1
        return self.book

    def set_margin_mode(self, symbol, mode):
        self.calls["margin_mode"].append((symbol, mode))

    def set_leverage(self, symbol, leverage):
        self.calls["leverage"].append((symbol, leverage))


RISK_CFG = {
    "max_position_pct": 10,
    "max_total_exposure_pct": 30,
    "leverage": 5,
    "max_leverage": 5,
    "daily_loss_limit_pct": 5.0,
    "min_equity_usdt": 20,
    "max_open_positions": 2,
    "min_fee_tolerance_pct": 0.15,
    "use_atr_trailing": True,
    "atr_trailing_mult": 1.5,
    "atr_stop_mult": 1.0,
    "atr_tp_mult": 2.5,
    "adaptive_sizing": True,
    "atr_normal_pct": 1.0,
    "size_volatility_bounds": [0.5, 2.0],
}


@pytest.fixture
def cfg():
    return dict(RISK_CFG)


@pytest.fixture
def ex():
    return FakeExchange()


# -- Composition -------------------------------------------------------


def test_engine_composes_all_default_policies(cfg, ex):
    eng = RiskEngine(cfg, ex)
    names = {p.name for p in eng.policies.values()}
    assert names == {
        "position_sizing",
        "exposure",
        "stop_loss",
        "take_profit",
        "leverage",
        "drawdown",
        "validation",
        "risk_based_sizing",
        "rr_validation",
    }
    assert len(eng.policies) == 9


def test_validation_policy_receives_peers(cfg, ex):
    eng = RiskEngine(cfg, ex)
    assert eng.validation.exposure is eng.exposure
    assert eng.validation.drawdown is eng.drawdown


def test_policy_injection_overrides_default(cfg, ex):
    class TinySizing(PositionSizingPolicy):
        def compute_position_size(self, symbol, price, equity, direction, atr=None):
            return 0.001

    eng = RiskEngine(cfg, ex, policies={"position_sizing": TinySizing(cfg)})
    assert eng.compute_position_size("X", 100, 1000, "buy") == 0.001
    assert isinstance(eng.position_sizing, TinySizing)


def test_injected_policy_registered_and_peer_wiring_kept(cfg, ex):
    class FakeValidation(ValidationPolicy):
        def __init__(self, cfg):
            super().__init__(cfg, None, None, None)

        def can_open(self, symbol, positions, equity, price, side):
            return False, "injected"

    eng = RiskEngine(cfg, ex, policies={"validation": FakeValidation(cfg)})
    assert eng.validation is eng.policies["validation"]
    assert eng.can_open("X", [], 1000, 100, "LONG") == (False, "injected")


def test_policy_lookup(cfg, ex):
    eng = RiskEngine(cfg, ex)
    assert eng.policy("stop_loss").name == "stop_loss"
    assert eng.policy("nope") is None


# -- PositionSizingPolicy ----------------------------------------------


def test_position_sizing_base(cfg, ex):
    eng = RiskEngine(cfg, ex)
    assert eng.compute_position_size("X", 100, 1000, "buy") == pytest.approx(1.0)


def test_position_sizing_adaptive_lowers_size_on_high_atr(cfg, ex):
    cfg["atr_normal_pct"] = 1.0
    eng = RiskEngine(cfg, ex)
    base = eng.compute_position_size("X", 100, 1000, "buy")
    high_atr = eng.compute_position_size("X", 100, 1000, "buy", atr=4.0)
    assert high_atr < base
    assert high_atr == pytest.approx(1.0 * max(0.5, min(2.0, 0.01 / 0.04)))


def test_position_sizing_atr_clamped_to_bounds(cfg, ex):
    eng = RiskEngine(cfg, ex)
    tiny_atr = eng.compute_position_size("X", 100, 1000, "buy", atr=0.1)
    assert tiny_atr == pytest.approx(1.0 * 2.0)


# -- fixed-notional rule (balance < 100 USDT -> 5 USDT) -----------------


def fixed_cfg(**overrides):
    cfg = dict(RISK_CFG)
    cfg["fixed_notional_usdt"] = 5
    cfg["fixed_notional_max_equity_usdt"] = 100
    cfg.update(overrides)
    return cfg


def test_fixed_notional_small_account(cfg, ex):
    eng = RiskEngine(fixed_cfg(), ex)
    qty = eng.compute_position_size("X", 100, 25, "buy")
    assert qty == pytest.approx(0.05)
    assert eng.notional(25, 100) == pytest.approx(5.0)


def test_fixed_notional_capped_by_equity(cfg, ex):
    eng = RiskEngine(fixed_cfg(), ex)
    assert eng.notional(3, 100) == pytest.approx(3.0)
    assert eng.compute_position_size("X", 100, 3, "buy") == pytest.approx(0.03)


def test_risk_based_sizing_large_account(cfg, ex):
    eng = RiskEngine(fixed_cfg(), ex)
    assert eng.notional(1000, 100) == pytest.approx(100.0)
    assert eng.compute_position_size("X", 100, 1000, "buy") == pytest.approx(1.0)


def test_fixed_notional_off_by_default(cfg, ex):
    eng = RiskEngine(cfg, ex)
    assert eng.notional(25, 100) == pytest.approx(2.5)


def test_fixed_notional_threshold_boundary(cfg, ex):
    eng = RiskEngine(fixed_cfg(), ex)
    assert eng.notional(99.99, 100) == pytest.approx(5.0)
    assert eng.notional(100.0, 100) == pytest.approx(10.0)


class _PreciseClient:
    def __init__(self, precision=None, precision_mode=0):
        self.markets = {
            "SOL/USDT:USDT": {
                "precisionMode": precision_mode,
                "precision": {"amount": precision or 0.01},
                "limits": {"amount": {"min": 0.01}},
            }
        }


class _PreciseExchange:
    def __init__(self, precision=None, precision_mode=0):
        self.client = _PreciseClient(precision, precision_mode)


def test_fixed_notional_clears_min_cost_ceil(cfg, ex):
    eng = RiskEngine(fixed_cfg(), _PreciseExchange())
    price = 75.94
    qty = eng.compute_position_size("SOL/USDT:USDT", price, 25, "buy")
    assert qty == pytest.approx(0.07)
    assert qty * price >= 5.0


def test_ceil_respects_decimal_places(cfg, ex):
    eng = RiskEngine(fixed_cfg(), _PreciseExchange(precision=3, precision_mode=1))
    qty = eng.compute_position_size("SOL/USDT:USDT", 1.234, 25, "buy")
    assert qty == pytest.approx(round(math.ceil(5.0 / 1.234 * 1000) / 1000, 3))
    assert qty * 1.234 >= 5.0


def test_equity_for_notional_fixed_mode(cfg, ex):
    eng = RiskEngine(fixed_cfg(), ex)
    assert eng.equity_for_notional(5.0, 100) == pytest.approx(5.0)
    assert eng.equity_for_notional(3.0, 100) == pytest.approx(3.0)


def test_equity_for_notional_risk_mode_invariant(cfg, ex):
    eng = RiskEngine(fixed_cfg(), ex)
    for atr in (None, 0.5, 4.0):
        notional = eng.notional(1000, 100, atr)
        eq = eng.equity_for_notional(notional, 100, atr)
        assert eng.notional(eq, 100, atr) == pytest.approx(notional, rel=1e-9)


# -- ExposurePolicy ----------------------------------------------------


def test_exposure_ok_within_limit(cfg, ex):
    eng = RiskEngine(cfg, ex)
    positions = [{"contracts": 1.0, "notional": 100.0}]
    ok, total = eng.exposure_ok(positions, equity=1000)
    assert ok is True
    assert total == 100.0


def test_exposure_ok_over_limit(cfg, ex):
    eng = RiskEngine(cfg, ex)
    positions = [{"contracts": 1.0, "notional": 400.0}]
    ok, _ = eng.exposure_ok(positions, equity=1000)
    assert ok is False


def test_exposure_ok_fetches_balance_when_equity_none(cfg, ex):
    eng = RiskEngine(cfg, ex)
    ok, _ = eng.exposure_ok([{"contracts": 1.0, "notional": 100.0}])
    assert ok is True
    assert ex.calls["balance"] == 1


def test_pending_notional_includes_open_orders(cfg, ex):
    ex.open_orders = [{"amount": 2.0, "price": 100.0}]
    eng = RiskEngine(cfg, ex)
    assert eng.pending_notional() == 200.0
    assert eng._pending_notional() == 200.0


# -- StopLossPolicy / TakeProfitPolicy ---------------------------------


def test_stop_loss_long(cfg, ex):
    eng = RiskEngine(cfg, ex)
    sl = eng.build_stop_loss(100.0, "buy", atr=2.0)
    assert sl == pytest.approx(100.0 - 1.0 * 2.0)


def test_stop_loss_short(cfg, ex):
    eng = RiskEngine(cfg, ex)
    sl = eng.build_stop_loss(100.0, "sell", atr=2.0)
    assert sl == pytest.approx(100.0 + 1.0 * 2.0)


def test_stop_loss_invalid_atr_returns_none(cfg, ex):
    eng = RiskEngine(cfg, ex)
    assert eng.build_stop_loss(100.0, "buy", atr=None) is None


def test_take_profit_long(cfg, ex):
    eng = RiskEngine(cfg, ex)
    tp = eng.build_take_profit(100.0, "buy", atr=2.0)
    assert tp == pytest.approx(100.0 + 2.5 * 2.0)


def test_take_profit_short(cfg, ex):
    eng = RiskEngine(cfg, ex)
    tp = eng.build_take_profit(100.0, "sell", atr=2.0)
    assert tp == pytest.approx(100.0 - 2.5 * 2.0)


def test_stop_loss_capped_by_max_distance(cfg, ex):
    cfg["max_stop_distance_pct"] = 3.0
    eng = RiskEngine(cfg, ex)
    sl = eng.build_stop_loss(100.0, "buy", atr=20.0)
    assert sl == pytest.approx(100.0 - 3.0)


def test_stop_loss_fixed_pct_net_binds(cfg, ex):
    cfg["sl_fixed_pct"] = 2.5
    eng = RiskEngine(cfg, ex)
    sl = eng.build_stop_loss(100.0, "buy", atr=20.0)
    assert sl == pytest.approx(100.0 - 2.5)


def test_stop_loss_unbounded_without_caps(cfg, ex):
    eng = RiskEngine(cfg, ex)
    sl = eng.build_stop_loss(100.0, "buy", atr=20.0)
    assert sl == pytest.approx(100.0 - 20.0)


def test_take_profit_fixed_pct_binds(cfg, ex):
    cfg["tp_fixed_pct"] = 3.0
    eng = RiskEngine(cfg, ex)
    tp = eng.build_take_profit(100.0, "buy", atr=50.0)
    assert tp == pytest.approx(100.0 + 3.0)


def test_trailing_stop_atr_hit(cfg, ex):
    eng = RiskEngine(cfg, ex)
    assert eng.trailing_stop_hit("long", best_price=100.0, current=96.0, atr=2.0) is True
    assert eng.trailing_stop_hit("long", best_price=100.0, current=97.5, atr=2.0) is False


def test_trailing_stop_pct_fallback(cfg, ex):
    cfg["use_atr_trailing"] = False
    cfg["trailing_stop_pct"] = 1.0
    eng = RiskEngine(cfg, ex)
    assert eng.trailing_stop_hit("short", best_price=100.0, current=101.5) is True
    assert eng.trailing_stop_hit("short", best_price=100.0, current=100.5) is False


# -- LeveragePolicy ----------------------------------------------------


def test_enforce_leverage(cfg, ex):
    eng = RiskEngine(cfg, ex)
    lev = eng.enforce_leverage("X/USDT:USDT")
    assert lev == 5
    assert ex.calls["margin_mode"] == [("X/USDT:USDT", "isolated")]
    assert ex.calls["leverage"] == [("X/USDT:USDT", 5)]


def test_enforce_leverage_capped_by_max(cfg, ex):
    cfg["leverage"] = 20
    cfg["max_leverage"] = 5
    eng = RiskEngine(cfg, ex)
    assert eng.enforce_leverage("X") == 5


# -- DrawdownPolicy ----------------------------------------------------


def test_daily_loss_exceeded(cfg, ex):
    eng = RiskEngine(cfg, ex)
    eng.set_initial_equity(1000)
    assert eng.daily_loss_exceeded(955) is False
    assert eng.daily_loss_exceeded(951) is False
    assert eng.daily_loss_exceeded(950) is True
    assert eng.daily_loss_exceeded(940) is True
    assert eng.daily_loss_exceeded(0) is True


def test_daily_loss_requires_initial_equity(cfg, ex):
    eng = RiskEngine(cfg, ex)
    assert eng.daily_loss_exceeded(100) is False


def test_update_equity_tracks_peak(cfg, ex):
    eng = RiskEngine(cfg, ex)
    eng.set_initial_equity(1000)
    assert eng.peak_equity == 1000
    eng.update_equity(1200)
    assert eng.peak_equity == 1200
    eng.update_equity(1100)
    assert eng.peak_equity == 1200
    assert eng.initial_equity == 1000


def test_below_min_equity(cfg, ex):
    eng = RiskEngine(cfg, ex)
    assert eng.below_min_equity(19.9) is True
    assert eng.below_min_equity(20.0) is False


# -- ValidationPolicy --------------------------------------------------


def test_can_open_ok(cfg, ex):
    eng = RiskEngine(cfg, ex)
    allowed, reason = eng.can_open("X", [], 1000, 100, "LONG")
    assert allowed is True
    assert reason == "ok"


def test_can_open_daily_loss_blocks(cfg, ex):
    eng = RiskEngine(cfg, ex)
    eng.set_initial_equity(1000)
    allowed, reason = eng.can_open("X", [], 900, 100, "LONG")
    assert allowed is False
    assert reason == "daily loss limit reached"


def test_can_open_max_positions_blocks(cfg, ex):
    eng = RiskEngine(cfg, ex)
    positions = [
        {"contracts": 1.0, "notional": 100.0},
        {"contracts": 1.0, "notional": 100.0},
    ]
    allowed, reason = eng.can_open("X", positions, 1000, 100, "LONG")
    assert allowed is False
    assert reason == "max open positions reached"


def test_can_open_exposure_blocks(cfg, ex):
    eng = RiskEngine(cfg, ex)
    positions = [{"contracts": 1.0, "notional": 350.0}]
    allowed, reason = eng.can_open("X", positions, 1000, 100, "LONG")
    assert allowed is False
    assert reason == "max total exposure reached"


def test_check_fee_tolerance(cfg, ex):
    eng = RiskEngine(cfg, ex)
    ok, spread = eng.check_fee_tolerance("X")
    assert ok is True
    assert spread == pytest.approx((100.1 - 100.0) / 100.05 * 100.0, abs=0.01)


def test_check_fee_tolerance_empty_book(cfg, ex):
    ex.book = {}
    eng = RiskEngine(cfg, ex)
    ok, spread = eng.check_fee_tolerance("X")
    assert ok is False
    assert spread == float("inf")


# -- Backward compatibility --------------------------------------------


def test_risk_manager_is_risk_engine(cfg, ex):
    assert RiskManager is RiskEngine
    mgr = RiskManager(cfg, ex)
    assert isinstance(mgr, RiskEngine)


def test_full_legacy_api_preserved(cfg, ex):
    eng = RiskEngine(cfg, ex)
    eng.set_initial_equity(1000)
    assert eng.initial_equity == 1000
    assert eng.peak_equity == 1000
    assert eng.cfg is cfg
    assert eng.exchange is ex
    assert callable(eng.compute_position_size)
    assert callable(eng.exposure_ok)
    assert callable(eng.check_fee_tolerance)
    assert callable(eng.can_open)
    assert callable(eng.daily_loss_exceeded)
    assert callable(eng.below_min_equity)
    assert callable(eng.trailing_stop_hit)
    assert callable(eng.build_take_profit)
    assert callable(eng.build_stop_loss)
    assert callable(eng.enforce_leverage)
    assert callable(eng.update_equity)
    assert callable(eng._pending_notional)


def test_policies_importable():
    for cls in (
        PositionSizingPolicy,
        ExposurePolicy,
        StopLossPolicy,
        TakeProfitPolicy,
        LeveragePolicy,
        DrawdownPolicy,
        ValidationPolicy,
    ):
        assert cls.name
