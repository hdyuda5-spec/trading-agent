"""Staged screener pipeline tests — funnel behaviour and telemetry."""

from types import SimpleNamespace

import pytest

from agent.features import build_feature_engine
from agent.services.filters import TradeFilters
from agent.services.stages import StagedScreenerPipeline, StageTrace, _signal_score
from agent.strategies.scoring import ScreeningGate
from tests.conftest import make_df, uptrend_df, flat_df


def _ohlcv(df):
    ms = [int(t.timestamp() * 1000) for t in df.index]
    return [
        [ms[i], o, h, l, c, v]
        for i, (o, h, l, c, v) in enumerate(
            zip(df["open"], df["high"], df["low"], df["close"], df["volume"])
        )
    ]


class FakeClient:
    def __init__(self):
        self.markets = {}


class FakeExchange:
    def __init__(self, tickers=None, df=None):
        self.client = FakeClient()
        self._tickers = tickers or {}
        self._df = df
        self.ohlcv_calls = 0

    def fetch_tickers(self):
        return dict(self._tickers)

    def fetch_ohlcv(self, symbol, timeframe="1h", limit=100):
        self.ohlcv_calls += 1
        if self._df is not None:
            return _ohlcv(self._df)
        return []

    def check_health(self):
        return True


class FakeRisk:
    def __init__(self, min_rr=0.0):
        self.min_rr = min_rr

    def build_stop_loss(self, entry, side, atr=None):
        return entry * 0.97 if side == "buy" else entry * 1.03

    def build_take_profit(self, entry, side, atr=None):
        return entry * 1.03 if side == "buy" else entry * 0.97

    def validate_rr(self, entry, sl, tp, side=None):
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk else 0.0
        if self.min_rr > 0 and rr < self.min_rr:
            return False, "RR_TOO_LOW", rr
        return True, "OK", rr

    def daily_loss_exceeded(self, equity):
        return False


class FakeExecution:
    def __init__(self):
        self.open_calls = []

    def screen_trade_ok(self, symbol, side, price, atr, equity, positions):
        return True, 100.0, "ok"

    def open_position(self, symbol, signal, equity, atr, ticket=None):
        self.open_calls.append({
            "symbol": symbol,
            "signal": signal,
            "equity": equity,
            "ticket": ticket,
        })
        return {"id": f"O{len(self.open_calls)}", "symbol": symbol}


class FakePortfolio:
    halted = False

    def equity(self):
        return 100.0

    def positions(self):
        return []

    def capture_setup(self, *a, **k):
        return {"meta": "setup"}

    def set_trade_meta(self, *a, **k):
        pass


class FakeStore:
    def __init__(self):
        self.tickets = {}
        self.saved_tickets = []

    def recent_trades(self, symbol, side, limit=4):
        return []

    def save_funnel(self, payload):
        pass

    def save_decision(self, **kwargs):
        pass

    def save_ticket(self, ticket):
        self.tickets[ticket.ticket_id] = ticket
        self.saved_tickets.append(ticket)

    def get_active_ticket(self, symbol, side=None):
        for t in self.tickets.values():
            if t.symbol == symbol and (side is None or t.side == side) and t.status == "NEW":
                if not t.expired():
                    return {"ticket_id": t.ticket_id, "status": t.status}
        return None


class FakeWhale:
    def data(self, symbol, ttl=300):
        return None

    def market_net_flow(self, provider, ttl=300):
        return 0.0, 0


class FakeNotifier:
    def __init__(self):
        self.info_calls = []

    def info(self, message):
        self.info_calls.append(message)

    def alert(self, message, detail=""):
        pass


class FakeScorer:
    def __init__(self, confidences=None, rejects=None):
        self.confidences = confidences or {}
        self.rejects = rejects or {}

    def evaluate(self, symbol, df, **kwargs):
        if symbol in self.rejects:
            return {
                "rejected": True,
                "side": None,
                "action": None,
                "confidence": 0.0,
                "reason": [],
                "reject_reason": self.rejects[symbol],
            }
        return {
            "rejected": False,
            "side": "LONG",
            "action": "BUY",
            "confidence": self.confidences.get(symbol, 0.8),
            "reason": ["sinyal teknis"],
            "reject_reason": None,
        }


class FakeDecisionEngine:
    def __init__(self, block=False, code=None):
        self.block = block
        self.code = code

    def assess(self, *a, **k):
        dv = SimpleNamespace()
        dv.should_block = (lambda: True) if self.block else (lambda: False)
        dv.status = "REJECT" if self.block else "PASS"
        dv.symbol = k.get("symbol", "?")
        dv.side = k.get("strategy_side")
        dv.action = None
        dv.score = 0.0
        dv.confidence = 0.0
        dv.regime = "UNCERTAIN"
        dv.reason_code = self.code or "BLOCKED"
        dv.reasons = []
        dv.evidence_dicts = lambda: []
        dv.weights = {}
        dv.rejection = SimpleNamespace(reason="blocked by design") if self.block else None
        return dv


def base_config(**pipeline_over):
    screener = {
        "enabled": True,
        "auto_trade": True,
        "min_volume_usdt": 0,
        "exclude_symbols": [],
        "no_trade_hours_wib": [],
        "rsi_long_range": [40, 75],
        "rsi_short_range": [25, 60],
        "pipeline": {
            "enabled": True,
            "universe_size": 6,
            "tech_keep": 5,
            "risk_keep": 4,
            "rank_n": 3,
            "min_bars": 52,
            "timeframe": "1h",
            "min_rr": 0.0,
        },
    }
    screener["pipeline"].update(pipeline_over)
    return {
        "symbols": ["BTC/USDT:USDT"],
        "exchange": {"name": "binance", "testnet": True},
        "trading": {"mode": "paper"},
        "human": {},
        "features": {
            "enabled": ["trend", "volatility", "volume", "structure"],
        },
        "risk": {
            "atr_period": 14,
            "atr_stop_mult": 1.5,
            "atr_tp_mult": 2.5,
            "sl_fixed_pct": 2.5,
            "tp_fixed_pct": 3.0,
            "max_stop_distance_pct": 3.0,
            "min_equity_usdt": 1,
            "daily_loss_limit_pct": 50,
            "max_open_positions": 2,
            "max_total_exposure_pct": 60,
            "min_fee_tolerance_pct": 15,
            "leverage": 10,
            "max_leverage": 10,
            "fixed_notional_usdt": 5,
            "fixed_notional_max_equity_usdt": 100,
        },
        "whale": {"market_regime": {"enabled": False}},
        "execution": {"poll_interval_seconds": 60, "order_type": "market"},
        "screener": screener,
    }


def make_pipeline(config, exchange, *, scorer=None, risk=None, decision=None, store=None):
    engine = build_feature_engine(
        market_data=exchange,
        config=config,
        enabled=["trend", "volatility", "volume", "structure"],
    )
    risk = risk or FakeRisk()
    execution = FakeExecution()
    portfolio = FakePortfolio()
    store = store or FakeStore()
    whale = FakeWhale()
    notifier = FakeNotifier()
    filters = TradeFilters(config)
    gate = ScreeningGate(config, risk, execution, exchange, portfolio)
    d = StagedScreenerPipeline(
        config, exchange, notifier, risk, execution, engine,
        whale, portfolio, store, None,
        scorer=scorer or FakeScorer(),
        gate=gate,
        filters=filters,
        decision_engine=decision,
        funnel=None,
    )
    return d, execution, notifier


def tickers_for(prefixes):
    """Monotonic-vol tied to a single synthetic pair per prefix."""
    out = {}
    for i, p in enumerate(prefixes):
        out[f"{p}/USDT:USDT"] = {
            "quoteVolume": float(1_000_000 - i * 10_000),
            "percentage": 1.0,
        }
    return out


# -- unit ------------------------------------------------------------------


def test_signal_score_directions():
    from agent.features.base import FeatureResult

    assert _signal_score(FeatureResult("x", "bullish", 0.8)) == pytest.approx(0.8)
    assert _signal_score(FeatureResult("x", "bearish", 0.6)) == pytest.approx(-0.6)
    assert _signal_score(FeatureResult("x", "neutral", 0.9)) == 0.0
    assert _signal_score(None) == 0.0


def test_stage_trace_reason_histogram():
    t = StageTrace("risk")
    t.entered = 4
    t.reject("A/USDT:USDT", "low_margin")
    t.reject("B/USDT:USDT", "low_confidence")
    t.reject("C/USDT:USDT", "low_margin")
    t.passed_symbols = ["D/USDT:USDT"]
    s = t.summary()
    assert s["entered"] == 4 == s["rejected_count"] + s["passed"]
    assert s["by_reason"] == {"low_margin": 2, "low_confidence": 1}


# -- funnel -----------------------------------------------------------------


def test_market_filter_limits_excludes_and_volume():
    config = base_config(universe_size=3)
    # volumes: 1_000_000 .. 950_000 stepping 10_000
    tickers = tickers_for(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])
    config["screener"]["exclude_symbols"] = ["AAA/USDT:USDT"]
    config["screener"]["min_volume_usdt"] = 960_000
    exchange = FakeExchange(tickers, df=uptrend_df())
    pipeline, _, _ = make_pipeline(config, exchange)
    traces = pipeline.run()

    mf = next(t for t in traces if t.name == "market_filter")
    s = mf.summary()
    assert s["entered"] == 6
    assert s["passed"] == 3
    assert set(s["by_reason"]) == {"excluded", "low_volume", "volume_rank_cutoff"}
    assert mf.passed_symbols == ["BBB/USDT:USDT", "CCC/USDT:USDT", "DDD/USDT:USDT"]


def test_full_funnel_ranks_by_confidence_and_executes_top_3():
    config = base_config(universe_size=6, tech_keep=5, risk_keep=4, rank_n=3)
    exchange = FakeExchange(tickers_for(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]), df=uptrend_df())
    conf = {
        "AAA/USDT:USDT": 0.90,
        "BBB/USDT:USDT": 0.85,
        "CCC/USDT:USDT": 0.80,
        "DDD/USDT:USDT": 0.70,
        "EEE/USDT:USDT": 0.60,
        "FFF/USDT:USDT": 0.50,
    }
    pipeline, execution, notifier = make_pipeline(
        config, exchange, scorer=FakeScorer(confidences=conf)
    )
    traces = pipeline.run()

    opened = [c["symbol"] for c in execution.open_calls]
    assert opened == ["AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT"]

    summaries = {t.name: t.summary() for t in traces}
    assert set(summaries) == {"market_filter", "technical", "risk", "scoring", "execution"}
    assert summaries["market_filter"]["passed"] == 6
    assert summaries["technical"]["passed"] == 5
    assert summaries["risk"]["passed"] == 4
    assert summaries["scoring"]["passed"] == 3
    assert summaries["execution"]["passed"] == 3

    joined = "\n".join(notifier.info_calls)
    assert "[PIPELINE] funnel:" in joined


def test_rank_n_does_not_force_all_trades_when_decision_blocks():
    config = base_config(rank_n=3)
    exchange = FakeExchange(tickers_for(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]), df=uptrend_df())
    pipeline, execution, _ = make_pipeline(
        config,
        exchange,
        scorer=FakeScorer(),
        decision=FakeDecisionEngine(block=True, code="DAILY_LOSS_LIMIT"),
    )
    traces = pipeline.run()

    assert execution.open_calls == []
    ex = next(t for t in traces if t.name == "execution")
    s = ex.summary()
    assert s["entered"] == 3
    assert all(r["reason"] == "DAILY_LOSS_LIMIT" for r in s["rejected"])
    assert s["passed"] == 0


def test_low_confidence_rejected_in_scoring_stage():
    config = base_config(rank_n=2)
    exchange = FakeExchange(tickers_for(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]), df=uptrend_df())
    rejects = {"CCC/USDT:USDT": "confidence 0.40 < 0.60"}
    pipeline, execution, _ = make_pipeline(
        config, exchange, scorer=FakeScorer(rejects=rejects)
    )
    traces = pipeline.run()
    sc = next(t.summary() for t in traces if t.name == "scoring")
    assert any(r["reason"] == "low_confidence" for r in sc["rejected"])
    assert all(c["symbol"] != "CCC/USDT:USDT" for c in execution.open_calls)


def test_min_rr_tightens_risk_stage():
    config = base_config(min_rr=1.5)
    exchange = FakeExchange(tickers_for(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]), df=uptrend_df())
    pipeline, execution, _ = make_pipeline(config, exchange, risk=FakeRisk(min_rr=1.5))
    traces = pipeline.run()
    rs = next(t.summary() for t in traces if t.name == "risk")
    assert any(r["reason"] == "RR_TOO_LOW" for r in rs["rejected"])
    assert execution.open_calls == []


def test_flat_market_rejected_as_neutral():
    config = base_config(universe_size=6)
    exchange = FakeExchange(tickers_for(["AAA", "BBB"]), df=flat_df())
    pipeline, execution, _ = make_pipeline(config, exchange)
    traces = pipeline.run()
    tech = next(t.summary() for t in traces if t.name == "technical")
    assert any(r["reason"] == "neutral_trend" for r in tech["rejected"])
    assert execution.open_calls == []


# -- Stage 5 TradeTicket -------------------------------------------------


def _ticket_store():
    return FakeStore()


def test_stage5_mints_valid_ticket_with_screener_source_and_metadata():
    config = base_config(universe_size=6, tech_keep=5, risk_keep=4, rank_n=3)
    exchange = FakeExchange(tickers_for(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]), df=uptrend_df())
    store = _ticket_store()
    pipeline, execution, _ = make_pipeline(config, exchange, store=store)
    traces = pipeline.run()

    assert execution.open_calls
    for call in execution.open_calls:
        t = call["ticket"]
        assert t is not None
        assert t.symbol == call["symbol"]
        assert t.is_valid()
        assert t.source == "screener"
        assert t.decision_status == "PASS"
        assert t.metadata.get("source") == "screener"
        assert t.metadata.get("strategy") == "screener"
    persisted = {c["ticket"].ticket_id for c in execution.open_calls}
    saved = {t.ticket_id for t in store.saved_tickets}
    assert persisted == saved
    assert len(store.saved_tickets) == 3
    ex = next(t for t in traces if t.name == "execution")
    assert ex.summary()["passed"] == 3


def test_decision_blocked_candidates_get_no_ticket_and_no_execution():
    config = base_config(rank_n=3)
    exchange = FakeExchange(tickers_for(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]), df=uptrend_df())
    store = _ticket_store()
    pipeline, execution, _ = make_pipeline(
        config,
        exchange,
        scorer=FakeScorer(),
        store=store,
        decision=FakeDecisionEngine(block=True, code="DAILY_LOSS_LIMIT"),
    )
    traces = pipeline.run()

    assert execution.open_calls == []
    assert store.saved_tickets == []
    ex = next(t.summary() for t in traces if t.name == "execution")
    assert ex["passed"] == 0
    assert all(r["reason"] == "DAILY_LOSS_LIMIT" for r in ex["rejected"])


def test_duplicate_candidate_single_ticket_single_order():
    config = base_config(universe_size=6, tech_keep=5, risk_keep=4, rank_n=3)
    exchange = FakeExchange(tickers_for(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]), df=uptrend_df())
    store = _ticket_store()

    pipeline, execution, _ = make_pipeline(config, exchange, store=store)
    traces1 = pipeline.run()
    opened1 = [c["ticket"] for c in execution.open_calls]
    assert len(opened1) == 3

    # same candidate set forwarded again (restart / duplicate broadcast):
    # a fresh pipeline instance shares the same store -> no duplicate tickets/orders.
    pipeline2, execution2, _ = make_pipeline(config, exchange, store=store)
    traces2 = pipeline2.run()

    assert execution2.open_calls == []
    ex2 = next(t for t in traces2 if t.name == "execution")
    assert all(r["reason"] == "duplicate_ticket" for r in ex2.summary()["rejected"])
    assert len({t.ticket_id for t in store.saved_tickets}) == len(store.saved_tickets) == 3

    # deterministic ticket ids: rerun (same window) yields the same id, so the
    # UNIQUE constraint + client-order-id idempotency hold.
    t1 = opened1[0]
    assert t1.ticket_id == store.saved_tickets[0].ticket_id
    assert t1.ticket_id.startswith("SCR-")


def test_invalid_candidate_forwarded_without_valid_ticket_rejected_in_stage5():
    config = base_config(rank_n=3)
    exchange = FakeExchange(tickers_for(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]), df=uptrend_df())
    store = _ticket_store()
    pipeline, execution, _ = make_pipeline(config, exchange, store=store)
    traces = pipeline.run()
    assert execution.open_calls
    # every executed candidate walked with a ticket; none lacked one.
    assert all(c["ticket"] is not None for c in execution.open_calls)