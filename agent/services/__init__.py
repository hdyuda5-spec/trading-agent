"""Service layer for the trading orchestrator.

The old ``TradingBot`` was a monolithic 800-line class mixing market data,
strategy evaluation, risk, order execution and reporting. The services in this
package own the business logic; ``agent.bot.TradingBot`` is now a thin
orchestrator that wires them together around an event bus:

    WebSocket feed
        -> EventBus
        -> CandleStore -> FeatureEngine -> strategies/DecisionEngine
        -> RiskEngine -> ExecutionService -> PortfolioService

Public surface:

- ``EventBus`` / ``Event`` — async pub/sub backbone.
- ``MarketFeed`` — Binance futures WebSocket (REST poll fallback).
- ``CandleStore`` — event-driven OHLCV cache.
- ``PortfolioService`` — positions/equity/trailing/trade lifecycle.
- ``ExecutionService`` — order placement + grid + preflight.
- ``SignalService`` — live strategy evaluation (momentum/ai_signal).
- ``TradeFilters`` — shared pre-trade gates.
- ``ScreenerService`` / ``WhaleService`` / ``ReportingService`` — periodic jobs.
"""

from agent.services.candle_store import CandleStore
from agent.services.event_bus import Event, EventBus
from agent.services.execution import ExecutionService
from agent.services.filters import TradeFilters
from agent.services.market_feed import (
    MarketFeed,
    normalize_symbol,
    parse_kline,
    parse_mark_price,
)
from agent.services.portfolio import PortfolioService
from agent.services.reporting import ReportingService
from agent.services.screener_service import ScreenerService
from agent.services.signals import SignalService
from agent.services.whale_service import WhaleService

__all__ = [
    "CandleStore",
    "Event",
    "EventBus",
    "ExecutionService",
    "MarketFeed",
    "PortfolioService",
    "ReportingService",
    "ScreenerService",
    "SignalService",
    "TradeFilters",
    "WhaleService",
    "normalize_symbol",
    "parse_kline",
    "parse_mark_price",
]
