"""TradeStore schema v2 tests: decisions, tickets, missed trades, funnel, journal."""

import pytest

from agent.data.trades import TradeStore
from agent.decision.ticket import TradeTicket


@pytest.fixture
def store(tmp_path):
    s = TradeStore(str(tmp_path / "trades.db"))
    yield s
    s.close()


def test_record_trade_full_with_v2_columns(store):
    store.record_trade_full(
        symbol="X/USDT", side="long", entry=100.0, exit_px=105.0, qty=1.0,
        pnl=5.0, pnl_pct=5.0, reason="tp", ticket_id="T1", order_id="O1",
        exchange_order_id="EO1", fees=0.1, slippage=0.01, regime="RANGING",
        score=6.5, win=1, pnl_after_fees=4.7,
    )
    metrics = store.metrics()
    assert metrics["trades"] == 1
    assert metrics["net_pnl"] == pytest.approx(5.0)


def test_ticket_persistence_roundtrip(store):
    t = TradeTicket.new("X/USDT:USDT", "LONG", "strat", 100.0,
                        stop_loss=99.0, take_profit=103.0, position_size=2.0,
                        reasons=["score 6"], evidence=[{"source": "trend"}])
    store.save_ticket(t)
    t.mark_filled("oid", "eoid", "MARKET")
    store.update_ticket_status(t.ticket_id, "FILLED", "oid", "eoid")

    row = store.get_ticket(t.ticket_id)
    assert row["status"] == "FILLED"
    assert row["order_id"] == "oid"
    assert row["exchange_order_id"] == "eoid"


def test_ticket_persists_screener_metadata_columns(store):
    t = TradeTicket.new("X/USDT:USDT", "LONG", "screener", 100.0,
                        stop_loss=99.0, take_profit=103.0, position_size=1.0,
                        ttl_seconds=300, ticket_id="SCR-ABC123",
                        equity=120.0, source="screener", decision_status="PASS",
                        metadata={"source": "screener", "strategy": "screener"})
    store.save_ticket(t)

    row = store.get_ticket(t.ticket_id)
    assert row["equity"] == pytest.approx(120.0)
    assert row["source"] == "screener"
    assert row["decision_status"] == "PASS"


def test_get_active_ticket_blocks_live_new_but_not_expired(store):
    live = TradeTicket.new("X/USDT:USDT", "LONG", "screener", 100.0, ttl_seconds=300,
                           ticket_id="SCR-LIVE")
    store.save_ticket(live)
    active = store.get_active_ticket("X/USDT:USDT", "LONG")
    assert active is not None
    assert active["ticket_id"] == "SCR-LIVE"

    store.update_ticket_status(live.ticket_id, "CANCELLED")
    assert store.get_active_ticket("X/USDT:USDT", "LONG") is None

    expired = TradeTicket(ticket_id="SCR-EXP", symbol="X/USDT:USDT", side="LONG",
                          strategy="screener", entry=100.0, created_at=100, expires_at=101)
    store.save_ticket(expired)
    assert store.get_active_ticket("X/USDT:USDT", "LONG") is None
    assert store.get_ticket("SCR-EXP")["status"] == "NEW"


def test_decision_trail(store):
    store.save_decision(symbol="X", status="PASS", action="BUY", score=6.5,
                        confidence=0.7, regime="TRENDING_UP", reasons=["trend up"])
    store.save_decision(symbol="X", status="REJECT", reason_code="INVALID_PRICE")
    summary = store.decision_summary()
    assert summary["by_status"]["PASS"] == 1
    assert summary["by_status"]["REJECT"] == 1
    assert any(d["reason_code"] == "INVALID_PRICE" for d in summary["by_reason_code"])


def test_missed_trades(store):
    store.save_missed_trade({"symbol": "X", "side": "LONG", "strategy": "s",
                             "reason_code": "INVALID_PRICE", "reason": "price 0",
                             "price": 0.0, "score": 3.0})
    s = store.missed_trades_summary()
    assert s["by_reason_code"][0] == {"reason_code": "INVALID_PRICE", "count": 1}
    assert len(s["recent"]) == 1


def test_funnel_roundtrip(store):
    store.save_funnel({"strategy_signals": 10, "rejected": {"SCORE_TOO_LOW": 3}})
    loaded = store.load_funnel()
    assert loaded["strategy_signals"] == 10
    assert loaded["rejected"]["SCORE_TOO_LOW"] == 3


def test_journal_and_reviewer(store):
    from agent.decision.reviewer import TradeReviewer

    r = TradeReviewer(store=store)
    exp_id = r.record_journal({"symbol": "X", "side": "LONG", "entry": 100.0,
                               "pnl": 5.0, "reason": "tp", "exit_time": 123.0})
    assert exp_id is not None
    summary = store.journal_summary()
    assert len(summary["recent"]) == 1
    assert summary["recent"][0]["label"] == "TP_HIT"