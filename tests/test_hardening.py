"""Phase 27/29/30 tests: config validation, paper exchange, secret redaction."""

import pytest

from agent.core.config_validate import ConfigError, validate_config, validate_or_raise
from agent.core.secrets import redact_api_keys, redact_common
from agent.execution.paper import PaperExchange


# ------------------------------------------------------------- config


def _base_cfg(**over):
    cfg = {
        "exchange": {"name": "binance", "testnet": True},
        "symbols": ["X/USDT:USDT"],
        "trading": {"mode": "paper"},
        "execution": {"order_type": "market"},
        "risk": {"min_equity_usdt": 20, "daily_loss_limit_pct": 5, "leverage": 5},
        "screener": {"auto_trade": False},
    }
    cfg.update(over)
    return cfg


def test_valid_config_passes():
    assert validate_config(_base_cfg()) == []


def test_invalid_exchange_and_mode():
    problems = validate_config(_base_cfg(exchange={"name": "kraken"}, trading={"mode": "atomic"}))
    assert any("exchange.name" in p for p in problems)
    assert any("trading.mode" in p for p in problems)


def test_negative_risk_fails():
    problems = validate_config(_base_cfg(risk={"min_equity_usdt": -5}))
    assert any("min_equity_usdt" in p for p in problems)


def test_live_auto_trade_warning():
    problems = validate_config(_base_cfg(trading={"mode": "live"}, screener={"auto_trade": True}))
    assert any("LIVE trading" in p for p in problems)


def test_live_auto_trade_ok_with_confirmation():
    cfg = _base_cfg(
        trading={"mode": "live", "confirm_live_auto_trade": True},
        screener={"auto_trade": True},
    )
    assert validate_config(cfg) == []


def test_validate_or_raise():
    with pytest.raises(ConfigError):
        validate_or_raise(_base_cfg(trading={"mode": "bogus"}))


# ------------------------------------------------------------- paper


class FakeReal:
    def __init__(self):
        self.testnet = False
        self.client = None
        self.px = 100.0

    def fetch_ticker(self, symbol):
        return {"last": self.px}

    def fetch_ohlcv(self, *a, **k):
        return []

    def fetch_order_book(self, *a, **k):
        return {"bids": [[self.px, 1]], "asks": [[self.px, 1]]}

    def fetch_tickers(self):
        return {"X/USDT:USDT": {"quoteVolume": 100}}


def make_paper(balance=1000.0):
    return PaperExchange(FakeReal(), initial_balance_usdt=balance, fee_pct=0.0)


def test_buy_updates_balance_and_position():
    p = make_paper()
    p.create_order("X/USDT:USDT", "market", "buy", 10.0)
    assert p.snapshot()["balance_usdt"] == pytest.approx(1000.0)  # equity: unchanged on open
    pos = p.snapshot()["positions"]["X/USDT:USDT"]
    assert pos["side"] == "long" and pos["contracts"] == 10.0


def test_stop_order_persists_and_cancel():
    p = make_paper()
    o = p.create_stop_order("X/USDT:USDT", "sell", 99.0, amount=10.0)
    assert o["status"] == "open"
    assert len(p.fetch_open_stop_orders("X/USDT:USDT")) == 1
    p.cancel_stop_order(o["id"])
    assert len(p.fetch_open_stop_orders("X/USDT:USDT")) == 0


def test_roundtrip_profit_updates_balance():
    p = make_paper()
    p.create_order("X/USDT:USDT", "market", "buy", 10.0)
    p.real.px = 110.0
    p.create_order("X/USDT:USDT", "market", "sell", 10.0)  # reduce-only close
    snap = p.snapshot()
    assert snap["balance_usdt"] == pytest.approx(1000.0 + 100.0)  # realized +100
    assert "X/USDT:USDT" not in snap["positions"]


def test_position_unrealized_mark():
    p = make_paper()
    p.create_order("X/USDT:USDT", "market", "buy", 10.0)
    p.real.px = 105.0
    poses = p.fetch_positions()
    assert poses[0]["unrealizedPnl"] == pytest.approx(50.0)


# ------------------------------------------------------------- redaction


def test_redact_common_removes_long_blobs():
    s = "api key abcdef0123456789abcdef01 dan oke"
    assert "abcdef0123456789abcdef01" not in redact_common(s)
    assert "oke" in redact_common(s)


def test_redact_api_keys():
    out = redact_api_keys("token=super-secret-value-h323", {"BINANCE_API_KEY": "super-secret-value-h323"})
    assert "super-secret-value-h323" not in out


def test_redact_common_masks_key_value():
    out = redact_common("BINANCE SECRET: abc_def_GHI_123_456")
    assert "abc_def_GHI_123_456" not in out