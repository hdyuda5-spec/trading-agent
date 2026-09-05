"""Decision Engine, Evidence, TradeTicket, Regime, Telemetry, Reviewer.

The decision layer turns market data + features + strategies into an
*explainable, auditable* verdict and — when it PASSES — a risk-checked
``TradeTicket`` that the execution layer is the only component allowed to act
on. Nothing here touches the exchange.
"""