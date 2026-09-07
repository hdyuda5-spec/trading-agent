"""Staged screener pipeline for auto-trade (configurable funnel).

Transforms the auto-trade flow from a single inline filter into an explicit,
configurable 5-stage funnel so every candidate's journey is auditable:

    100+ USDT-M pairs
      -> Stage 1 market_filter   (keep ``universe_size`` = 50 by volume)
      -> Stage 2 technical       (Technical + Liquidity + Structure, keep 20)
      -> Stage 3 risk            (Risk / RR / regime, keep 8)
      -> Stage 4 scoring         (ScreenerScorer rank, keep ``rank_n`` = 3)
      -> Stage 5 TradeTicket     (Risk Engine may reject some or all; NOT forced)

Defaults follow the documented funnel but are fully configurable under
``screener.pipeline`` and are never hard-coded. ``rank_n`` ranks candidates by
confidence — it does NOT force N trades. All existing risk controls, SL/TP
guards, exposure limits, decision-engine vetoes and auto-trade safeguards in
``ScreeningGate`` / ``UnifiedDecisionEngine`` / ``RiskEngine`` stay in force.

Telemetry: every stage logs ``entered / passed / rejected`` with reason codes,
and the run summary is persisted to the trade store so the funnel can be
inspected afterwards.
"""

import hashlib
import logging
import time

from agent.core.utils import is_valid_atr, ohlcv_to_dataframe
from agent.decision.ticket import TradeTicket
from agent.services.filters import TradeFilters
from agent.strategies.scoring import ScreenerScorer, ScreeningGate

logger = logging.getLogger("trading-agent")

TRACE_PREFIX = "[PIPELINE]"

_DEFAULTS = {
    "enabled": True,
    "universe_size": 50,
    "tech_keep": 20,
    "risk_keep": 8,
    "rank_n": 3,
    "min_bars": 52,
    "timeframe": "1h",
    "min_rr": 0.0,          # 0 disables RR hard-reject; >0 (e.g. 1.5) tightens it
}

# Stage-2 technical/liquidity/structure weights (direction score in [-1,1]).
_TECH_WEIGHTS = {
    "structure": 0.30,
    "liquidity": 0.25,
    "trend": 0.25,
    "volume": 0.20,
}

_FACTOR_FEATURE = {
    "structure": "market_structure",
    "liquidity": "smart_money_liquidity",
    "trend": "trend",
    "volume": "volume",
}

_BULLISH = {"bullish", "liquid", "rising"}
_BEARISH = {"bearish", "illiquid", "falling"}


class StageTrace:
    """Per-stage telemetry: how many entered, passed, and rejected (codes)."""

    def __init__(self, name: str):
        self.name = name
        self.entered = 0
        self.passed_symbols = []
        self.rejected = []  # list of {"symbol", "reason", "detail"}

    def reject(self, symbol, reason, detail=""):
        self.rejected.append({"symbol": symbol, "reason": reason, "detail": detail})

    def summary(self) -> dict:
        by_reason: dict = {}
        for r in self.rejected:
            key = r["reason"]
            by_reason[key] = by_reason.get(key, 0) + 1
        return {
            "stage": self.name,
            "entered": self.entered,
            "passed": len(self.passed_symbols),
            "rejected_count": len(self.rejected),
            "by_reason": {k: v for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])},
            "rejected": self.rejected[:200],  # cap for persistence readability
        }


def _signal_score(fr) -> float:
    """Signed confidence in [-1,1] from a FeatureResult (neutral -> 0)."""
    if fr is None or fr.signal in ("neutral", "unavailable"):
        return 0.0
    try:
        conf = min(1.0, max(0.0, float(fr.confidence)))
    except (TypeError, ValueError):
        conf = 0.0
    if fr.signal in _BULLISH:
        return conf
    if fr.signal in _BEARISH:
        return -conf
    return 0.0


class StagedScreenerPipeline:
    """Executes the 5-stage auto-trade funnel for one screen pass."""

    def __init__(self, config, exchange, notifier, risk, execution, feature_engine,
                 whale, portfolio, store, candles, *,
                 scorer=None, gate=None, filters=None, decision_engine=None,
                 funnel=None, missed_journal=None):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.risk = risk
        self.execution = execution
        self.feature_engine = feature_engine
        self.whale = whale
        self.portfolio = portfolio
        self.store = store
        self.candles = candles
        self.funnel = funnel
        self.missed_journal = missed_journal

        screener = config.get("screener", {}) or {}
        pipeline_cfg = screener.get("pipeline", {}) or {}
        self.cfg = dict(_DEFAULTS)
        self.cfg.update({k: v for k, v in pipeline_cfg.items() if v is not None})

        self.scorer = scorer or ScreenerScorer(config, exchange)
        self.gate = gate or ScreeningGate(config, risk, execution, exchange, portfolio)
        self.filters = filters or TradeFilters(config)
        self.decision_engine = decision_engine
        self._min_rr = float(self.cfg.get("min_rr", 0.0) or 0.0)
        self._min_bars = int(self.cfg.get("min_bars", 52))
        self._timeframe = str(self.cfg.get("timeframe") or screener.get("timeframe", "1h"))
        self._regime_cache = None
        self._minted = set()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    # -- public entrypoint ------------------------------------------------

    def run(self):
        traces = []
        self._minted = set()
        cands = self._stage_market_filter(traces)
        cands = self._stage_technical_filter(cands, traces)
        cands = self._stage_risk_filter(cands, traces)
        cands = self._stage_scoring_filter(cands, traces)
        self._stage_execute(cands, traces)
        self._log_traces(traces)
        self._persist(traces)
        return traces

    # -- Stage 1: market filter -------------------------------------------

    def _stage_market_filter(self, traces):
        sc = self.config.get("screener", {}) or {}
        exclude = set(sc.get("exclude_symbols", []) or [])
        min_vol = float(sc.get("min_volume_usdt", 0) or 0)
        universe_size = int(self.cfg.get("universe_size", 50))
        trace = StageTrace("market_filter")
        try:
            tickers = self.exchange.fetch_tickers() or {}
        except Exception as e:
            trace.reject("(universe)", "fetch_tickers_error", type(e).__name__)
            traces.append(trace)
            return []
        rows = []
        for symbol, t in tickers.items():
            if not str(symbol).endswith("/USDT:USDT"):
                continue
            trace.entered += 1
            if symbol in exclude:
                trace.reject(symbol, "excluded")
                continue
            qv = float(t.get("quoteVolume") or 0)
            if qv < min_vol:
                trace.reject(symbol, "low_volume")
                continue
            rows.append({
                "symbol": symbol,
                "qv": qv,
                "chg": float(t.get("percentage") or 0),
            })
        rows.sort(key=lambda r: -r["qv"])
        top = rows[:universe_size]
        for c in rows[universe_size:]:
            trace.reject(c["symbol"], "volume_rank_cutoff")
        if top:
            trace.passed_symbols = [c["symbol"] for c in top]
        traces.append(trace)
        return top

    # -- Stage 2: technical + liquidity + structure ------------------------

    def _stage_technical_filter(self, cands, traces):
        tech_keep = int(self.cfg.get("tech_keep", 20))
        trace = StageTrace("technical")
        trace.entered = len(cands)
        analyzed = []
        for cand in cands:
            symbol = cand["symbol"]
            df = self._fetch_df(symbol)
            if df is None or len(df) < self._min_bars:
                trace.reject(symbol, "insufficient_data")
                continue
            info = self._tech_analyze(symbol, df)
            if info is None:
                trace.reject(symbol, "analyze_failed")
                continue
            if info.get("side") == "NEUTRAL":
                trace.reject(symbol, "neutral_trend")
                continue
            if not is_valid_atr(info.get("atr")):
                trace.reject(symbol, "invalid_atr")
                continue
            cand = dict(cand)
            cand.update(info)
            cand["df"] = df
            analyzed.append(cand)
        analyzed.sort(key=lambda c: -float(c.get("_tech_score") or 0.0))
        kept = analyzed[:tech_keep]
        for c in analyzed[tech_keep:]:
            trace.reject(c["symbol"], "tech_rank_cutoff")
        if kept:
            trace.passed_symbols = [c["symbol"] for c in kept]
        traces.append(trace)
        return kept

    def _tech_analyze(self, symbol, df):
        try:
            fs = self.feature_engine.compute(symbol, df)
            frs = {
                factor: self._feature_source(fs, factor)
                for factor in _TECH_WEIGHTS
            }
            components = {k: _signal_score(frs[k]) for k in _TECH_WEIGHTS}
            direction = sum(_TECH_WEIGHTS[k] * components[k] for k in _TECH_WEIGHTS)
            if abs(direction) < 1e-6:
                side = "NEUTRAL"
            else:
                side = "LONG" if direction > 0 else "SHORT"
            close = df["close"]
            atr = self._atr_of(fs)
            return {
                "side": side,
                "price": float(close.iloc[-1]),
                "atr": round(atr, 8) if is_valid_atr(atr) else None,
                "rsi": self._rsi_of(fs),
                "vol": self._vol_of(fs),
                "pattern": self._pattern_of(fs),
                "smart_money": self._smart_money_of(fs),
                "_tech_score": round(direction, 4),
                "_tech_components": {k: round(v, 4) for k, v in components.items()},
            }
        except Exception as e:
            logger.debug("%s analyze %s failed: %s", TRACE_PREFIX, symbol, e)
            return None

    def _feature_source(self, fs, factor):
        name = _FACTOR_FEATURE[factor]
        fr = fs.get(name)
        if factor == "structure" and (fr is None or fr.signal == "neutral"):
            fr = fs.get("structure")
        elif factor == "liquidity" and (fr is None or fr.signal == "neutral"):
            fr = fs.get("liquidity")
        return fr

    # -- Stage 3: risk / RR / regime --------------------------------------

    def _stage_risk_filter(self, cands, traces):
        risk_keep = int(self.cfg.get("risk_keep", 8))
        trace = StageTrace("risk")
        trace.entered = len(cands)

        def reject_all(reason, detail=""):
            for c in cands:
                trace.reject(c["symbol"], reason, detail)
            traces.append(trace)
            return []

        if not self.filters.trading_hours_ok():
            return reject_all("no_trade_hours")
        try:
            equity = self.portfolio.equity()
        except Exception:
            return reject_all("equity_unavailable")
        if not self.gate.exchange_available():
            return reject_all("exchange_unavailable")
        ok, category, detail = self.gate.check_global(equity)
        if not ok:
            code = category or "risk_global"
            return reject_all(code, detail or "")

        try:
            positions = self.portfolio.positions()
        except Exception:
            positions = []
        open_symbols = {
            p.get("symbol")
            for p in positions
            if abs(float(p.get("contracts") or 0)) > 0 and p.get("symbol")
        }
        regime = self._market_regime()

        passed = []
        for cand in cands:
            symbol = cand["symbol"]
            side = cand["side"]
            if symbol in open_symbols:
                trace.reject(symbol, "position_open")
                continue
            if self.filters.losing_streak(symbol, side, self.store):
                trace.reject(symbol, "losing_streak")
                continue
            ok_whale, whale_msg = self.filters.whale_filter_ok(symbol, side, self.whale)
            if not ok_whale:
                trace.reject(symbol, "whale_conflict", whale_msg)
                continue
            if not self.filters.market_regime_allows(side, self.whale, self._whale_symbols):
                trace.reject(symbol, "market_regime", regime and f"net={regime[0]:+.0f}" or "")
                continue
            entry = float(cand.get("price") or 0)
            atr = cand.get("atr")
            buy_side = "buy" if side == "LONG" else "sell"
            sl = self.risk.build_stop_loss(entry, buy_side, atr)
            tp = self.risk.build_take_profit(entry, buy_side, atr)
            if sl is None or tp is None:
                trace.reject(symbol, "invalid_risk_levels")
                continue
            rr = None
            if self._min_rr > 0:
                ok_rr, code, value = self.risk.validate_rr(entry, sl, tp)
                if not ok_rr:
                    trace.reject(symbol, code, str(value))
                    continue
                rr = float(value)
            else:
                risk_dist = abs(entry - sl)
                reward_dist = abs(tp - entry)
                if risk_dist > 0:
                    rr = reward_dist / risk_dist
            cand = dict(cand)
            cand["sl"] = sl
            cand["tp"] = tp
            cand["rr"] = rr
            passed.append(cand)
        passed.sort(key=lambda c: (
            float(c.get("_tech_score") or 0.0),
            float(c.get("rr") or 0.0),
        ), reverse=True)
        kept = passed[:risk_keep]
        for c in passed[risk_keep:]:
            trace.reject(c["symbol"], "risk_rank_cutoff")
        if kept:
            trace.passed_symbols = [c["symbol"] for c in kept]
        traces.append(trace)
        return kept

    # -- Stage 4: scoring --------------------------------------------------

    def _stage_scoring_filter(self, cands, traces):
        rank_n = int(self.cfg.get("rank_n", 3))
        sc = self.config.get("screener", {}) or {}
        news = sc.get("news") or []
        trace = StageTrace("scoring")
        trace.entered = len(cands)
        passed = []
        for cand in cands:
            symbol = cand["symbol"]
            losing = self.filters.losing_streak(symbol, cand["side"], self.store)
            try:
                verdict = self.scorer.evaluate(
                    symbol,
                    cand.get("df"),
                    chg=cand.get("chg"),
                    news=news,
                    losing_streak=losing,
                    market_regime=self._market_regime(),
                )
            except Exception as e:
                trace.reject(symbol, "scoring_error", type(e).__name__)
                continue
            if verdict.get("rejected"):
                code = self._reject_code(verdict.get("reject_reason", ""))
                trace.reject(symbol, code, verdict.get("reject_reason") or "")
                continue
            cand = dict(cand)
            cand["verdict"] = verdict
            cand["confidence"] = float(verdict.get("confidence", 0.0))
            passed.append(cand)
        passed.sort(key=lambda c: -float(c.get("confidence") or 0.0))
        top = passed[:rank_n]
        for c in passed[rank_n:]:
            trace.reject(c["symbol"], "confidence_rank_cutoff")
        if top:
            trace.passed_symbols = [c["symbol"] for c in top]
        traces.append(trace)
        return top

    # -- Stage 5: TradeTicket -> Execution ---------------------------------

    def _stage_execute(self, cands, traces):
        trace = StageTrace("execution")
        trace.entered = len(cands)
        if not cands:
            traces.append(trace)
            return
        try:
            equity = self.portfolio.equity()
        except Exception:
            for c in cands:
                trace.reject(c["symbol"], "equity_unavailable")
            traces.append(trace)
            return
        try:
            positions = self.portfolio.positions()
        except Exception:
            positions = []
        for cand in cands:
            symbol = cand["symbol"]
            side = cand["side"]
            verdict = cand.get("verdict") or {}
            price = float(cand.get("price") or 0)
            atr = cand.get("atr") or 0.0
            ok, category, detail, equity_eff = self.gate.check_order(
                symbol, side, price, atr, equity, positions
            )
            if not ok:
                code = category or "invalid_order"
                trace.reject(symbol, code, detail or "")
                continue
            dv = None
            if self.decision_engine is not None:
                dv = self.decision_engine.assess(
                    symbol, cand.get("df"), features=None,
                    strategy_side=side, signal_conf=verdict.get("confidence"),
                    equity=equity, positions=positions,
                    price=price, sl=cand.get("sl"), tp=cand.get("tp"), atr=atr,
                )
                self._record_decision(dv)
                if dv.should_block():
                    self._blocked(symbol, side, "screener", dv)
                    trace.reject(symbol, dv.reason_code or "decision_block",
                                 (dv.rejection.reason if dv.rejection else "") or "")
                    continue
            if (symbol, side) in self._minted:
                trace.reject(symbol, "duplicate_ticket", "sudah dibuat di run ini")
                continue
            existing = self._active_ticket(symbol, side)
            if existing is not None:
                trace.reject(symbol, "duplicate_ticket", existing.get("ticket_id") or "")
                continue
            signal = {
                "strategy": "screener",
                "symbol": symbol,
                "side": side,
                "action": verdict.get("action"),
                "confidence": verdict.get("confidence"),
                "price": price,
                "reason": [str(r) for r in verdict.get("reason", [])],
                "metadata": {
                    "rsi": cand.get("rsi"),
                    "vol": cand.get("vol"),
                    "chg": cand.get("chg"),
                    "confidence_score": verdict.get("confidence"),
                    "pattern": (cand.get("pattern") or {}).get("name"),
                    "smart_money": (cand.get("smart_money") or {}).get("direction"),
                },
            }
            setup = self.portfolio.capture_setup(
                symbol, side, price, atr, signal.get("metadata"), verdict.get("confidence")
            )
            self.portfolio.set_trade_meta(symbol, "screener", setup, verdict.get("confidence"))
            ticket = self._build_ticket(symbol, side, cand, verdict, dv, atr, equity, equity_eff)
            if ticket is None or not ticket.is_valid():
                trace.reject(symbol, "invalid_ticket", "TradeTicket gagal dibentuk")
                continue
            self._minted.add((symbol, side))
            self._persist_ticket(ticket)
            if self.funnel:
                self.funnel.inc("tickets_created")
                try:
                    self.funnel.save()
                except Exception:
                    pass
            order = self.execution.open_position(symbol, signal, equity_eff, atr, ticket=ticket)
            if order is not None:
                trace.passed_symbols.append(symbol)
            else:
                trace.reject(symbol, "execution_rejected")
        traces.append(trace)

    # -- decision trail helpers --------------------------------------------

    def _record_decision(self, dv):
        if self.store is None:
            return
        try:
            self.store.save_decision(
                symbol=dv.symbol, status=dv.status, action=dv.action,
                score=dv.score, confidence=dv.confidence, regime=dv.regime,
                reason_code=dv.reason_code, reasons=dv.reasons,
                evidence=dv.evidence_dicts(), weight=dv.weights,
            )
        except Exception:
            pass

    def _blocked(self, symbol, side, strategy, dv):
        code = dv.reason_code or "UNKNOWN"
        if self.funnel:
            self.funnel.reject(code)
            try:
                self.funnel.save()
            except Exception:
                pass
        if self.missed_journal is not None:
            try:
                self.missed_journal.record(
                    symbol, side, strategy, code,
                    dv.rejection.reason if dv.rejection else "",
                    score=dv.score,
                )
            except Exception:
                pass
        logger.info("%s [DECISION] %s blocked %s: %s (%s)", TRACE_PREFIX, symbol, side, code, dv.regime)

    # -- TradeTicket minting (Stage 5) ------------------------------------

    def _active_ticket(self, symbol, side):
        """Restart-safe duplicate guard: a live NEW ticket blocks re-entry."""
        if self.store is None:
            return None
        try:
            return self.store.get_active_ticket(symbol, side)
        except Exception:
            return None

    @staticmethod
    def _ticket_id(symbol, side, ttl):
        """Deterministic, idempotent ticket id stable within one TTL window.

        The same candidate re-forwarded in the same window (restart, duplicate
        broadcast) yields the same ticket_id, so the UNIQUE constraint and the
        OrderManager client-order-id idempotency both prevent duplicates.
        """
        bucket = int(time.time()) // max(int(ttl or 300), 1)
        raw = f"screener|{symbol}|{side}|{bucket}".encode("utf-8")
        return "SCR-" + hashlib.sha1(raw).hexdigest()[:12].upper()

    def _build_ticket(self, symbol, side, cand, verdict, dv, atr, equity, equity_eff):
        sl = cand.get("sl")
        tp = cand.get("tp")
        entry = float(cand.get("price") or 0)
        if entry <= 0:
            return None
        confidence = float(dv.confidence) if dv is not None else float(verdict.get("confidence") or 0.0)
        if dv is not None:
            score, regime = float(dv.score), dv.regime
            reasons = list(dv.reasons)
            evidence = dv.evidence_dicts()
            decision_status = dv.status
        else:
            score, regime = 0.0, None
            reasons = [str(r) for r in verdict.get("reason", [])]
            evidence = []
            decision_status = "PASS"
        risk_amount = size = rr = None
        try:
            risk_amount = self.risk.risk_per_trade_amount(equity_eff)
            size = self.risk.risk_position_size(symbol, entry, side, atr=atr, equity=equity_eff, stop_loss=sl)
            if sl and tp:
                _ok, _code, _val = self.risk.validate_rr(entry, sl, tp, side)
                rr = float(_val)
        except Exception:
            pass
        ttl = float((self.config.get("decision", {}) or {}).get("ticket_ttl_seconds", 300) or 300)
        leverage = getattr(self.risk, "cfg", {}).get("leverage", 1) if hasattr(self.risk, "cfg") else 1.0
        return TradeTicket.new(
            symbol, side, "screener", entry,
            stop_loss=sl, take_profit=tp,
            risk_pct=float((self.config.get("risk", {}) or {}).get("risk_per_trade_pct", 1.0)),
            risk_amount=float(risk_amount or 0.0),
            position_size=size,
            leverage=float(leverage),
            atr=atr, rr=rr,
            score=score, confidence=confidence, regime=regime,
            reasons=reasons, evidence=evidence,
            ttl_seconds=ttl,
            metadata={"source": "screener", "strategy": "screener", "symbol": symbol},
            ticket_id=self._ticket_id(symbol, side, ttl),
            equity=equity_eff, source="screener", decision_status=decision_status,
        )

    def _persist_ticket(self, ticket):
        if self.store is None:
            return
        try:
            self.store.save_ticket(ticket)
        except Exception:
            pass

    # -- telemetry ---------------------------------------------------------

    def _log_traces(self, traces):
        for t in traces:
            s = t.summary()
            reason_txt = ", ".join(f"{k}={v}" for k, v in s["by_reason"].items()) or "-"
            logger.info(
                "%s stage=%s entered=%d passed=%d rejected=%d reason[%s]",
                TRACE_PREFIX, s["stage"], s["entered"], s["passed"],
                s["rejected_count"], reason_txt,
            )
        self.notifier.info(
            f"{TRACE_PREFIX} funnel: " + " > ".join(
                f"{t.summary()['stage']}={t.summary()['passed']}" for t in traces
            )
        )

    def _persist(self, traces):
        if self.store is None:
            return
        try:
            payload = {t.name: t.summary() for t in traces}
            self.store.save_funnel({f"pipeline:last:{int(time.time())}": payload})
            self.store.save_funnel({"pipeline:last": payload})
        except Exception:
            pass

    # -- helpers -----------------------------------------------------------

    def _fetch_df(self, symbol):
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, self._timeframe, limit=100)
            return ohlcv_to_dataframe(ohlcv)
        except Exception:
            return None

    @staticmethod
    def _reject_code(reject_reason):
        r = str(reject_reason or "").lower()
        if "pump" in r:
            return "pump_chase"
        if "confidence" in r:
            return "low_confidence"
        if "margin" in r:
            return "low_margin"
        return "scoring_reject"

    def _atr_of(self, fs):
        try:
            return float(fs.get("volatility").metadata.get("atr"))
        except Exception:
            return None

    def _rsi_of(self, fs):
        try:
            return round(float(fs.get("trend").metadata.get("rsi")), 1)
        except Exception:
            return None

    def _vol_of(self, fs):
        try:
            return round(float(fs.get("volume").metadata.get("vol_ratio")), 1)
        except Exception:
            return None

    def _pattern_of(self, fs):
        try:
            return fs.get("structure").metadata.get("pattern")
        except Exception:
            return None

    def _smart_money_of(self, fs):
        try:
            return dict(fs.get("sentiment").metadata) or None
        except Exception:
            return None

    def _market_regime(self):
        if self._regime_cache is not None:
            return self._regime_cache
        try:
            if (self.config.get("whale", {}) or {}).get("market_regime", {}).get("enabled", False):
                self._regime_cache = self.whale.market_net_flow(self._whale_symbols)
            else:
                self._regime_cache = None
        except Exception:
            self._regime_cache = None
        return self._regime_cache

    def _whale_symbols(self):
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            tickers = {}
        symbols = list(self.config["symbols"])
        rows = [
            (s, float(t.get("quoteVolume") or 0))
            for s, t in tickers.items()
            if s.endswith("/USDT:USDT")
        ]
        rows.sort(key=lambda r: -r[1])
        for s, _ in rows[: int(self.config.get("whale", {}).get("max_coins", 15))]:
            if s not in symbols:
                symbols.append(s)
        return symbols