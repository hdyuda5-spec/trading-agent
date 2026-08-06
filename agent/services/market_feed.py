"""Event-driven market feed.

Primary transport is a Binance USDⓈ-M Futures WebSocket (mark-price + kline
streams) using ``aiohttp``. Each update is published on the event bus as a
``market.tick`` / ``market.candle`` event. When the socket cannot be kept
alive the feed degrades to a REST polling fallback that emits the same events,
so consumers never see the difference.

No business logic lives here: the feed only converts wire messages into
events. Symbol normalization and message parsing are pure functions.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from agent.services.event_bus import Event, EventBus

logger = logging.getLogger("trading-agent")

WS_MAINNET = "wss://fstream.binance.com"
WS_TESTNET = "wss://fstream.binancefuture.com"


def normalize_symbol(ws_symbol: str, exchange=None) -> str:
    """ccxt symbol from a Binance ws id (``"BTCUSDT"`` -> ``"BTC/USDT:USDT"``).

    Uses the exchange market table when available (authoritative), otherwise a
    deterministic fallback: uppercase, split base/quote on the trailing USDT.
    """
    if exchange is not None:
        try:
            markets = getattr(getattr(exchange, "client", None), "markets", {})
            for symbol, market in markets.items():
                if str(market.get("id") or "").lower() == str(ws_symbol).lower():
                    return symbol
        except Exception:
            pass
    raw = str(ws_symbol).upper()
    if ":" in raw or "/" in raw:
        return raw if ":USDT" in raw else f"{raw}:USDT"
    if raw.endswith("USDT") and len(raw) > 4:
        return f"{raw[:-4]}/USDT:USDT"
    return f"{raw}/USDT:USDT"


def parse_mark_price(msg: dict) -> Optional[Event]:
    """``{"e":"markPriceUpdate","s":..,"p":..}`` -> ``market.tick`` event."""
    price = msg.get("p")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if not price or price <= 0:
        return None
    return Event(
        topic="market.tick",
        data={
            "symbol": normalize_symbol(msg.get("s", "")),
            "price": price,
            "mark_price": price,
            "ts_ms": msg.get("T"),
        },
        source="feed.ws",
    )


def parse_kline(msg: dict) -> Optional[Event]:
    """``{"e":"kline","k":{...}}`` -> ``market.candle`` event."""
    k = msg.get("k") or {}
    try:
        candle = {
            "ts": int(k.get("t")),
            "open": float(k.get("o")),
            "high": float(k.get("h")),
            "low": float(k.get("l")),
            "close": float(k.get("c")),
            "volume": float(k.get("v")),
            "closed": bool(k.get("x")),
        }
    except (TypeError, ValueError):
        return None
    if not candle["close"] or not candle["ts"]:
        return None
    return Event(
        topic="market.candle",
        data={
            "symbol": normalize_symbol(k.get("s") or msg.get("s", "")),
            "timeframe": k.get("i", ""),
            "ts": candle["ts"],
            "candle": candle,
        },
        source="feed.ws",
    )


class MarketFeed:
    """Publishes ``market.tick`` / ``market.candle`` events on the bus."""

    def __init__(self, exchange, config: dict, bus: EventBus):
        self.exchange = exchange
        self.bus = bus
        self.symbols: List[str] = list(config.get("symbols") or [])
        self.timeframe: str = config.get("timeframe") or "15m"
        self.testnet: bool = bool(getattr(exchange, "testnet", False))
        exec_cfg = config.get("execution") or {}
        self.poll_interval: int = int(exec_cfg.get("poll_interval_seconds", 60))
        self.max_reconnect_retries: int = int(config.get("feed", {}).get("max_reconnect_retries", 8))
        self._running = False
        self._fallback = False
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def ws_enabled(self) -> bool:
        return not self._fallback

    @property
    def fallback(self) -> bool:
        return self._fallback

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        try:
            await self._ws_loop()
        except Exception as e:
            logger.warning("MarketFeed websocket aborted: %s", e)
        if self._running and not self._fallback:
            self._fallback = True
        if self._running:
            await self._poll_loop()

    async def stop(self) -> None:
        self._running = False
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- websocket -------------------------------------------------------

    def _ws_symbol(self, symbol: str) -> str:
        try:
            market = self.exchange.client.market(symbol)
            return str(market["id"]).lower()
        except Exception:
            return str(symbol).replace("/", "").replace(":", "").lower()

    def _stream_url(self) -> str:
        streams = []
        for symbol in self.symbols:
            sym = self._ws_symbol(symbol)
            streams.append(f"{sym}@markPrice@1s")
            streams.append(f"{sym}@kline_{self.timeframe}")
        base = WS_TESTNET if self.testnet else WS_MAINNET
        return f"{base}/stream?streams=" + "/".join(streams)

    async def _ws_loop(self) -> None:
        retries = 0
        self._session = aiohttp.ClientSession()
        try:
            while self._running:
                try:
                    url = self._stream_url()
                    async with self._session.ws_connect(url, heartbeat=20, ssl=False) as ws:
                        retries = 0
                        logger.info(
                            "MarketFeed ws connected (%d symbols, %s%s)",
                            len(self.symbols),
                            "testnet " if self.testnet else "",
                            self.timeframe,
                        )
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                except Exception as e:
                    if not self._running:
                        return
                    logger.warning("MarketFeed ws error: %s", e)
                retries += 1
                if retries > self.max_reconnect_retries:
                    logger.warning("MarketFeed ws reconnect exhausted -> polling fallback")
                    self._fallback = True
                    return
                await asyncio.sleep(min(2 ** min(retries, 5), 30))
        finally:
            await self._session.close()
            self._session = None

    async def _handle_message(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except (ValueError, TypeError):
            return
        if msg.get("e") == "markPriceUpdate":
            event = parse_mark_price(msg)
            if event is not None:
                await self.bus.publish(event)
        elif msg.get("e") == "kline":
            event = parse_kline(msg)
            if event is not None:
                await self.bus.publish(event)

    # -- fallback polling ------------------------------------------------

    async def _poll_loop(self) -> None:
        logger.warning(
            "MarketFeed polling fallback: REST every %ss for %d symbols",
            self.poll_interval,
            len(self.symbols),
        )
        while self._running:
            for symbol in self.symbols:
                try:
                    ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
                    last = float(ticker.get("last") or 0)
                    if last > 0:
                        await self.bus.publish(
                            Event(
                                topic="market.tick",
                                data={"symbol": symbol, "price": last, "mark_price": last},
                                source="feed.poll",
                            )
                        )
                except Exception as e:
                    logger.warning("MarketFeed poll ticker %s failed: %s", symbol, e)
                try:
                    ohlcv = await asyncio.to_thread(
                        self.exchange.fetch_ohlcv, symbol, self.timeframe, 200
                    )
                    if ohlcv:
                        last = ohlcv[-1]
                        ts, o, h, l, c, v = (last + [0])[:6]
                        await self.bus.publish(
                            Event(
                                topic="market.candle",
                                data={
                                    "symbol": symbol,
                                    "timeframe": self.timeframe,
                                    "ts": int(ts),
                                    "candle": {
                                        "ts": int(ts),
                                        "open": float(o),
                                        "high": float(h),
                                        "low": float(l),
                                        "close": float(c),
                                        "volume": float(v),
                                        "closed": True,
                                    },
                                },
                                source="feed.poll",
                            )
                        )
                except Exception as e:
                    logger.warning("MarketFeed poll ohlcv %s failed: %s", symbol, e)
            await asyncio.sleep(self.poll_interval)
