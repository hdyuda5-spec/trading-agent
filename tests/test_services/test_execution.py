"""ExecutionService safeguard: no valid TradeTicket, no order forwarding."""

import time

import pytest

from agent.core.risk import RiskEngine
from agent.decision.ticket import TradeTicket
from agent.services.execution import ExecutionService


class FakeClient:
    def __init__(self, markets=None):
        self.markets = markets or {
            "X/USDT:USDT": {
                "limits": {"cost": {"min": 5.0}, "amount": {"min": 0.01}}
            }
        }

    def amount_to_precision(self, symbol, qty):
        return round(qty, 6)


class FakeExchange:
    def __init__(self, client=None):
        self.client = client or FakeClient()
        self.book = {"bids": [[100.0, 5.0]], "asks": [[100.1, 5.0]]}

    def fetch_order_book(self, symbol, limit=5):
        return self.book

    def fetch_open_orders(self):
        return []


RISK_CFG = {
    "max_position_pct": 10,
    "max_total_exposure_pct": 30,
    "leverage": 5,
    "max_leverage": 5,
    "daily_loss_limit_pct": 5.0,
    "min_equity_usdt": 20,
    "max_open_positions": 2,
    "min_fee_tolerance_pct": 0.15,
    "adaptive_sizing": True,
    "fixed_notional_usdt": 5,
    "fixed_notional_max_equity_usdt": 100,
}


def make_service(**risk_overrides):
    cfg = {
        "risk": dict(RISK_CFG, **risk_overrides),
        "execution": {},
    }
    ex = FakeExchange()
    risk = RiskEngine(cfg["risk"], ex)
    svc = ExecutionService(
        cfg, ex, risk,
        orders=None, notifier=None, strategies=[], whale=None, portfolio=None,
    )
    return svc


def test_fixed_notional_small_account():
    svc = make_service()
    ok, equity_eff, reason = svc.screen_trade_ok("X/USDT:USDT", "LONG", 100.0, None, 25.0, [])
    assert ok, reason
    assert equity_eff == pytest.approx(5.0)
    qty = svc.risk.compute_position_size("X/USDT:USDT", 100.0, equity_eff, "buy")
    assert qty == pytest.approx(0.05)
    assert qty * 100.0 == pytest.approx(5.0)


def test_fixed_notional_respects_min_cost():
    svc = make_service()
    client = FakeClient(
        markets={"X/USDT:USDT": {"limits": {"cost": {"min": 6.0}, "amount": {"min": 0.01}}}}
    )
    ex = FakeExchange(client)
    risk = RiskEngine(dict(RISK_CFG), ex)
    svc = ExecutionService(
        {"risk": RISK_CFG, "execution": {}}, ex, risk,
        orders=None, notifier=None, strategies=[], whale=None, portfolio=None,
    )
    ok, _, reason = svc.screen_trade_ok("X/USDT:USDT", "LONG", 100.0, None, 25.0, [])
    assert not ok
    assert "minCost" in reason


def test_risk_based_sizing_large_account():
    svc = make_service()
    ok, equity_eff, reason = svc.screen_trade_ok("X/USDT:USDT", "LONG", 100.0, None, 1000.0, [])
    assert ok, reason
    assert equity_eff == pytest.approx(1000.0)
    qty = svc.risk.compute_position_size("X/USDT:USDT", 100.0, equity_eff, "buy")
    assert qty == pytest.approx(1.0)


class FakeOrders:
    def __init__(self, result=None):
        self.result = result or {"id": "O1", "symbol": "X/USDT:USDT"}
        self.calls = []

    def open_position(self, symbol, signal, equity, atr=None, ticket=None):
        self.calls.append({"symbol": symbol, "ticket": ticket})
        return self.result


def make_exec_service(orders):
    cfg = {"risk": dict(RISK_CFG), "execution": {}}
    ex = FakeExchange()
    risk = RiskEngine(cfg["risk"], ex)
    svc = ExecutionService(
        cfg, ex, risk, orders=orders, notifier=None,
        strategies=[], whale=None, portfolio=None,
    )
    return svc


def _signal():
    return {"strategy": "screener", "symbol": "X/USDT:USDT", "side": "LONG",
            "action": "BUY", "price": 100.0, "confidence": 0.8, "reason": []}


def test_open_position_requires_ticket():
    orders = FakeOrders()
    svc = make_exec_service(orders)
    assert svc.open_position("X/USDT:USDT", _signal(), 100.0, None, ticket=None) is None
    assert orders.calls == []


def test_open_position_rejects_expired_or_non_new_ticket():
    orders = FakeOrders()
    svc = make_exec_service(orders)
    expired = TradeTicket(ticket_id="T-EXP", symbol="X/USDT:USDT", side="LONG", strategy="s",
                          entry=100.0, created_at=100, expires_at=101)
    assert not expired.is_valid()
    assert svc.open_position("X/USDT:USDT", _signal(), 100.0, None, ticket=expired) is None
    assert orders.calls == []

    filled = TradeTicket.new("X/USDT:USDT", "LONG", "s", 100.0)
    filled.mark_filled("oid")
    assert not filled.is_valid()
    assert svc.open_position("X/USDT:USDT", _signal(), 100.0, None, ticket=filled) is None
    assert orders.calls == []


def test_open_position_forward_valid_ticket_to_order_manager():
    orders = FakeOrders()
    svc = make_exec_service(orders)
    ticket = TradeTicket.new("X/USDT:USDT", "LONG", "screener", 100.0,
                             stop_loss=99.0, take_profit=103.0, ttl_seconds=300)
    order = svc.open_position("X/USDT:USDT", _signal(), 100.0, 1.0, ticket=ticket)
    assert order is not None
    assert orders.calls == [{"symbol": "X/USDT:USDT", "ticket": ticket}]
    assert order["id"] == "O1"
