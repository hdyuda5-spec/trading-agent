"""PortfolioService: equity baseline, management loop and trade lifecycle."""

import pytest

from agent.core.risk import RiskEngine
from agent.services.candle_store import CandleStore
from agent.services.event_bus import Event, EventBus
from agent.services.portfolio import PortfolioService

SYMBOL = "BNB/USDT:USDT"


class FakeStore:
    def __init__(self):
        self.state = {}
        self.trades = []
        self.experiences = []

    def load_state(self, key, default=None):
        return self.state.get(key, default)

    def save_state(self, key, value):
        self.state[key] = value

    def record_trade(self, *args, **kwargs):
        self.trades.append((args, kwargs))

    def add_experience(self, *args, **kwargs):
        self.experiences.append((args, kwargs))
        return len(self.experiences)


class FakeNotifier:
    def __init__(self):
        self.messages = []
        self.alerts = []
        self.closes = []

    def info(self, *a):
        self.messages.append(a)

    def alert(self, *a):
        self.alerts.append(a)

    def send_close(self, *a):
        self.closes.append(a)


class FakeReflector:
    def __init__(self):
        self.calls = []

    def reflect_async(self, *a):
        self.calls.append(a)


class FakeOrders:
    def __init__(self):
        self.closed = []
        self.guarded = []

    def close_all(self, symbol):
        self.closed.append(symbol)

    def ensure_sl_tp(self, *a):
        self.guarded.append(a)


class FakeExchange:
    def __init__(self, balance=1000.0, positions=None, tickers=None, ticker=None, trades=None):
        self.balance_usdt = balance
        self.positions = positions or []
        self.tickers = tickers or {}
        self.ticker = ticker or {"last": 100.0}
        self.trades = trades or []

    def fetch_balance(self):
        return {"USDT": {"total": self.balance_usdt, "free": self.balance_usdt}}

    def fetch_positions(self, symbols=None):
        if symbols:
            wanted = set(symbols)
            return [p for p in self.positions if p.get("symbol") in wanted]
        return list(self.positions)

    def fetch_tickers(self):
        return self.tickers

    def fetch_ticker(self, symbol):
        return self.ticker

    def fetch_my_trades(self, symbol, limit=5):
        return list(self.trades)


def long_pos(price=100.0, contracts=1.0, symbol=SYMBOL):
    return {"symbol": symbol, "contracts": contracts, "entryPrice": price, "side": "long"}


@pytest.fixture
def risk_cfg():
    return {
        "max_position_pct": 10,
        "max_total_exposure_pct": 60,
        "max_open_positions": 3,
        "leverage": 5,
        "max_leverage": 10,
        "daily_loss_limit_pct": 5,
        "min_equity_usdt": 20,
        "trailing_stop_pct": 5,
        "atr_period": 14,
    }


@pytest.fixture
def svc(risk_cfg):
    store = FakeStore()
    notifier = FakeNotifier()
    reflector = FakeReflector()
    orders = FakeOrders()
    exchange = FakeExchange()
    bus = EventBus()
    candles = CandleStore()
    risk = RiskEngine(risk_cfg, exchange)
    portfolio = PortfolioService(
        {"risk": {"atr_period": 14}, "execution": {"reduce_only_on_close": True}},
        exchange, risk, orders, store, notifier, reflector, candles, bus,
    )
    portfolio._store = store
    portfolio._notifier = notifier
    portfolio._orders = orders
    portfolio._exchange = exchange
    portfolio._reflector = reflector
    portfolio._candles = candles
    return portfolio


class TestEquityBaseline:
    def test_new_day_sets_baseline(self, svc):
        svc.ensure_equity_baseline(500.0)
        baseline = svc._store.state["equity_baseline"]
        assert baseline["equity"] == 500.0
        assert svc.risk.initial_equity == 500.0

    def test_same_day_no_reset(self, svc):
        svc.ensure_equity_baseline(500.0)
        svc.ensure_equity_baseline(510.0)
        assert svc._store.state["equity_baseline"]["equity"] == 500.0

    def test_topup_resets_baseline(self, svc):
        import time

        svc._store.save_state("equity_baseline", {"date": time.strftime("%Y-%m-%d"), "equity": 100.0})
        svc.ensure_equity_baseline(130.0)
        baseline = svc._store.state["equity_baseline"]
        assert baseline["equity"] == 130.0
        assert svc.risk.initial_equity == 130.0
        assert any("top-up" in str(m) for m in svc._notifier.messages)

    def test_initial_equity_from_saved_baseline(self, svc):
        import time

        svc._store.save_state("equity_baseline", {"date": time.strftime("%Y-%m-%d"), "equity": 200.0})
        svc.ensure_equity_baseline(150.0)
        assert svc.risk.initial_equity == 200.0


class TestPositions:
    def test_pos_side_via_side(self, svc):
        assert svc.pos_side({"side": "long", "contracts": 1}) == "long"
        assert svc.pos_side({"side": "short", "contracts": 1}) == "short"

    def test_pos_side_via_position_amt(self, svc):
        pos = {"contracts": 1, "info": {"positionAmt": "-5"}}
        assert svc.pos_side(pos) == "short"

    def test_open_position_skips_flat(self, svc):
        positions = [{"symbol": SYMBOL, "contracts": 0, "side": "long"}, long_pos()]
        opened = svc.open_position(SYMBOL, positions)
        assert opened["side"] == "LONG"

    def test_open_position_none_when_missing(self, svc):
        assert svc.open_position(SYMBOL, [{"symbol": "OTHER", "contracts": 1}]) is None

    def test_set_and_pop_trade_meta(self, svc):
        svc.set_trade_meta(SYMBOL, "momentum", {"entry": 100.0}, 0.7)
        strategy, setup = svc.pop_trade_meta(SYMBOL)
        assert strategy == "momentum"
        assert setup["confidence"] == 0.7

    def test_capture_setup_shape(self, svc):
        s = svc.capture_setup(SYMBOL, "buy", 100.0, 1.0, metadata={"rsi": 30}, confidence=0.8)
        assert s["symbol"] == SYMBOL
        assert s["entry"] == 100.0
        assert s["metadata"] == {"rsi": 30}
        assert s["confidence"] == 0.8


class TestManage:
    def test_trailing_stop_closes_position(self, svc):
        svc.risk.set_initial_equity(1000.0)
        pos = long_pos(100.0)
        svc._exchange.positions = [pos]
        svc._exchange.tickers = {SYMBOL: {"last": 100.0}}
        svc.manage(1000.0, [pos])
        assert svc.trailing[SYMBOL]["peak"] == 100.0

        closed = []
        svc.bus.subscribe("trade.closed", lambda e: closed.append(e.data))
        svc._exchange.tickers = {SYMBOL: {"last": 90.0}}
        svc.manage(1000.0, [pos])
        assert svc._orders.closed == [SYMBOL]
        assert len(svc._store.trades) == 1
        args, _ = svc._store.trades[0]
        assert args[7] == "trailing"
        assert svc._notifier.closes
        assert closed and closed[0]["reason"] == "trailing"

    def test_daily_loss_halts_and_closes(self, svc):
        svc.risk.set_initial_equity(1000.0)
        pos = long_pos(100.0)
        svc._exchange.positions = [pos]
        svc.manage(950.0, [pos])
        assert svc.halted is True
        assert any("Daily loss limit" in str(a) for a in svc._notifier.alerts)
        assert svc._orders.closed == [SYMBOL]

    def test_daily_loss_alert_fires_once_while_halted(self, svc):
        svc.risk.set_initial_equity(1000.0)
        pos = long_pos(100.0)
        svc.manage(950.0, [pos])
        svc.manage(940.0, [])
        assert svc.halted is True
        assert sum("Daily loss limit" in str(a) for a in svc._notifier.alerts) == 1

    def test_daily_loss_unhalts_when_equity_recovers(self, svc):
        svc.risk.set_initial_equity(1000.0)
        svc.manage(950.0, [])
        assert svc.halted is True
        svc.manage(990.0, [])
        assert svc.halted is False

    def test_guard_sl_tp_skips_when_disabled(self, svc):
        svc.config = {"execution": {"reduce_only_on_close": False}}
        svc._exchange.positions = [long_pos()]
        svc.guard_sl_tp()
        assert svc._orders.guarded == []

    def test_reconcile_stale_trailing_records_trade(self, svc):
        svc.trailing[SYMBOL] = {"side": "long", "peak": 110.0, "entry": 100.0}
        svc._exchange.ticker = {"last": 115.0}
        svc._exchange.trades = [{"side": "sell", "price": 115.0, "amount": 1.0, "timestamp": 0}]
        svc.reconcile_stale_trailing()
        assert SYMBOL not in svc.trailing
        assert len(svc._store.trades) == 1
        assert svc._store.trades[0][0][7] == "sl_tp"


class TestConcurrency:
    def test_trailing_snapshot_is_isolated_copy(self, svc):
        svc.trailing[SYMBOL] = {"side": "long", "peak": 100.0, "entry": 95.0}
        snap = svc.trailing_snapshot()
        snap[SYMBOL]["peak"] = 999.0
        assert svc.trailing[SYMBOL]["peak"] == 100.0

    def test_concurrent_mutation_and_snapshot_no_runtime_error(self, svc):
        import threading

        stop = threading.Event()
        errors = []

        def mutator(prefix):
            try:
                i = 0
                while not stop.is_set():
                    sym = f"{prefix}/{i}/USDT"
                    svc.set_trade_meta(sym, "screener", {"entry": 100.0 + i}, 0.5)
                    svc.trailing[sym] = {"side": "long", "peak": 101.0 + i, "entry": 100.0 + i}
                    svc.pop_trade_meta(sym)
                    svc.trailing.pop(sym, None)
                    i += 1
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=mutator, args=(f"SYM{t}",)) for t in range(4)]
        for t in threads:
            t.start()
        try:
            for _ in range(200):
                svc.trailing_snapshot()
        finally:
            stop.set()
        for t in threads:
            t.join(timeout=5)
        assert errors == []


class TestClosePosition:
    def test_records_pnl_and_publishes_event(self, svc):
        pos = long_pos(100.0)
        svc._exchange.positions = [pos]
        events = []
        svc.bus.subscribe("trade.closed", lambda e: events.append(e))
        svc.close_position(pos, "manual", 95.0)
        assert svc._orders.closed == [SYMBOL]
        args, _ = svc._store.trades[0]
        symbol, side, entry, price, contracts, pnl, pnl_pct, reason = args[:8]
        assert (symbol, side, entry, price, reason) == (SYMBOL, "long", 100.0, 95.0, "manual")
        assert pnl == -5.0
        assert pnl_pct == -5.0
        assert events and events[0].data["symbol"] == SYMBOL
