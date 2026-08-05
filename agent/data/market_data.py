"""Market data abstraction.

`MarketData` is a Protocol describing the read-only market data surface that
features and strategies depend on. `ExchangeClient` satisfies it structurally,
which lets the Feature Engine be tested with fakes without touching the network.

Only read operations live here; order placement stays in the execution layer.
"""

from typing import Any, List, Optional, Protocol, Sequence


class MarketData(Protocol):
    """Read-only market data interface (implemented by ExchangeClient)."""

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "15m", limit: int = 200, since: Optional[int] = None
    ) -> List[list]: ...

    def fetch_ticker(self, symbol: str) -> dict: ...

    def fetch_tickers(self) -> dict: ...

    def fetch_order_book(self, symbol: str, limit: int = 5) -> dict: ...

    def fetch_trades(self, symbol: str, limit: int = 500) -> Sequence[dict]: ...

    def fetch_funding_rate(self, symbol: str) -> Optional[float]: ...


def has_funding_rate(market_data: Any) -> bool:
    return callable(getattr(market_data, "fetch_funding_rate", None))
