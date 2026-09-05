import json
import os
import sqlite3
import threading
from datetime import datetime, timezone


class TradeStore:
    def __init__(self, path="data/trades.db"):
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._init()

    @staticmethod
    def _columns(table, db):
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}

    def _init(self):
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                strategy TEXT,
                entry REAL, exit REAL, qty REAL,
                pnl REAL, pnl_pct REAL,
                reason TEXT
            )
            """
        )
        cols = self._columns("trades", self._db)
        if "confidence" not in cols:
            self._db.execute("ALTER TABLE trades ADD COLUMN confidence REAL")
        # -- schema v2 migrations (safe, additive only) --
        for col, decl in (
            ("ticket_id", "TEXT"),
            ("order_id", "TEXT"),
            ("exchange_order_id", "TEXT"),
            ("fees", "REAL"),
            ("slippage", "REAL"),
            ("regime", "TEXT"),
            ("score", "REAL"),
            ("sharpe", "REAL"),
            ("sortino", "REAL"),
            ("max_drawdown", "REAL"),
            ("exit_reason", "TEXT"),
            ("win", "INTEGER"),
            ("pnl_after_fees", "REAL"),
        ):
            if col not in cols:
                self._db.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated INTEGER
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS experience (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                strategy TEXT,
                setup TEXT,
                outcome TEXT,
                exit_reason TEXT,
                pnl REAL,
                pnl_pct REAL,
                lesson TEXT
            )
            """
        )
        # -- schema v2: decision trail, tickets, missed trades, funnel --
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                action TEXT,
                score REAL,
                confidence REAL,
                regime TEXT,
                reason_code TEXT,
                reasons TEXT,
                evidence TEXT,
                weight TEXT
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                ticket_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                strategy TEXT,
                entry REAL,
                stop_loss REAL,
                take_profit REAL,
                risk_pct REAL,
                risk_amount REAL,
                position_size REAL,
                notional REAL,
                rr REAL,
                score REAL,
                confidence REAL,
                regime TEXT,
                status TEXT,
                order_id TEXT,
                exchange_order_id TEXT,
                reasons TEXT,
                evidence TEXT,
                expires_at REAL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS missed_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT,
                strategy TEXT,
                reason_code TEXT,
                reason TEXT,
                price REAL,
                score REAL,
                evidence TEXT,
                metadata TEXT
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated INTEGER
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                symbol TEXT,
                side TEXT,
                entry REAL,
                pnl REAL,
                pnl_pct REAL,
                reason TEXT,
                label TEXT,
                ticket_id TEXT,
                misc TEXT
            )
            """
        )
        self._db.commit()

    def record_trade(self, symbol, side, entry, exit_px, qty, pnl, pnl_pct, reason, strategy="bot", confidence=None):
        ts = int(datetime.now(timezone.utc).timestamp())
        with self._lock:
            self._db.execute(
                "INSERT INTO trades (ts,symbol,side,strategy,entry,exit,qty,pnl,pnl_pct,reason,confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, symbol, side, strategy, entry, exit_px, qty, pnl, pnl_pct, reason, confidence),
            )
            self._db.commit()

    def save_state(self, key, value):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO state (key,value,updated) VALUES (?,?,?)",
                (key, json.dumps(value), int(datetime.now(timezone.utc).timestamp())),
            )
            self._db.commit()

    def add_experience(self, symbol, side, strategy, setup, outcome, exit_reason, pnl, pnl_pct, lesson):
        ts = int(datetime.now(timezone.utc).timestamp())
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO experience (ts,symbol,side,strategy,setup,outcome,exit_reason,pnl,pnl_pct,lesson) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, symbol, side, strategy, json.dumps(setup), outcome, exit_reason, pnl, pnl_pct, lesson),
            )
            self._db.commit()
            return cur.lastrowid

    def update_experience(self, exp_id, lesson):
        with self._lock:
            self._db.execute(
                "UPDATE experience SET lesson=? WHERE id=?", (lesson, exp_id)
            )
            self._db.commit()

    def recent_lessons(self, limit=6):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM experience ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                f"[{r[3]} {r[2]} via {r[4] or '-'}] {r[10]} "
                f"(pnl {r[8]:+.2f} USDT, {r[9]:+.1f}%)"
            )
        return out

    def recent_trades(self, symbol, side, limit=4):
        with self._lock:
            rows = self._db.execute(
                "SELECT pnl, ts FROM trades WHERE symbol=? AND side=? "
                "ORDER BY id DESC LIMIT ?",
                (symbol, side.lower(), limit),
            ).fetchall()
        return [{"pnl": float(r[0]), "ts": r[1]} for r in rows]

    def load_state(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def metrics(self):
        with self._lock:
            rows = self._db.execute("SELECT pnl FROM trades").fetchall()
        if not rows:
            return {}
        pnls = [float(r[0]) for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "trades": len(pnls),
            "win_rate": round(len(wins) / len(pnls) * 100, 1),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "net_pnl": round(sum(pnls), 4),
            "avg_win": round(gross_win / len(wins), 4) if wins else 0,
            "avg_loss": round(gross_loss / len(losses), 4) if losses else 0,
        }

    def confidence_breakdown(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT confidence, pnl, strategy FROM trades WHERE confidence IS NOT NULL"
            ).fetchall()
        if not rows:
            return []
        ranges = [(0, 65, "0-65"), (65, 75, "65-75"), (75, 90, "75-90"), (90, 101, "90+")]
        out = []
        for lo, hi, label in ranges:
            bucket = [r for r in rows if lo <= float(r[0]) < hi]
            if not bucket:
                continue
            pnls = [float(r[1]) for r in bucket]
            wins = [p for p in pnls if p > 0]
            strategies = {}
            for r in bucket:
                strategies[r[2]] = strategies.get(r[2], 0) + 1
            out.append(
                {
                    "range": label,
                    "trades": len(pnls),
                    "wins": len(wins),
                    "win_rate": round(len(wins) / len(pnls) * 100, 1),
                    "net_pnl": round(sum(pnls), 4),
                    "strategies": ", ".join(f"{k}={v}" for k, v in sorted(strategies.items())),
                }
            )
        return out

    def close(self):
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------
    # schema v2 APIs
    # ------------------------------------------------------------------

    def record_trade_full(self, *, symbol, side, entry, exit_px, qty, pnl, pnl_pct,
                          reason=None, strategy="bot", confidence=None, ticket_id=None,
                          order_id=None, exchange_order_id=None, fees=None,
                          slippage=None, regime=None, score=None, exit_reason=None,
                          win=None, pnl_after_fees=None, ts=None):
        cols = ["ts", "symbol", "side", "strategy", "entry", "exit", "qty",
                "pnl", "pnl_pct", "reason", "confidence"]
        vals = [
            ts or int(datetime.now(timezone.utc).timestamp()), symbol, side, strategy,
            entry, exit_px, qty, pnl, pnl_pct, reason, confidence,
        ]
        extras = {
            "ticket_id": ticket_id, "order_id": order_id, "exchange_order_id": exchange_order_id,
            "fees": fees, "slippage": slippage, "regime": regime, "score": score,
            "exit_reason": exit_reason, "win": win, "pnl_after_fees": pnl_after_fees,
        }
        for k, v in extras.items():
            if v is not None:
                cols.append(k)
                vals.append(v)
        ph = ",".join("?" * len(cols))
        colnames = ",".join(cols)
        with self._lock:
            self._db.execute(
                f"INSERT INTO trades ({colnames}) VALUES ({ph})", vals
            )
            self._db.commit()

    def save_decision(self, *, symbol, status, action=None, score=None, confidence=None,
                      regime=None, reason_code=None, reasons=None, evidence=None, weight=None):
        ts = int(datetime.now(timezone.utc).timestamp())
        with self._lock:
            self._db.execute(
                "INSERT INTO decisions (ts,symbol,status,action,score,confidence,regime,"
                "reason_code,reasons,evidence,weight) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, symbol, status, action, score, confidence, regime, reason_code,
                 json.dumps(reasons or []), json.dumps(evidence or []), json.dumps(weight or {})),
            )
            self._db.commit()

    def save_ticket(self, ticket):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO tickets (created_at,ticket_id,symbol,side,strategy,"
                "entry,stop_loss,take_profit,risk_pct,risk_amount,position_size,notional,rr,"
                "score,confidence,regime,status,order_id,exchange_order_id,reasons,evidence,"
                "expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ticket.created_at, ticket.ticket_id, ticket.symbol, ticket.side,
                    ticket.strategy, ticket.entry, ticket.stop_loss, ticket.take_profit,
                    ticket.risk_pct, ticket.risk_amount, ticket.position_size,
                    ticket.notional, ticket.rr, ticket.score, ticket.confidence,
                    ticket.regime, ticket.status, ticket.order_id,
                    ticket.exchange_order_id, json.dumps(ticket.reasons),
                    json.dumps(ticket.evidence), ticket.expires_at,
                ),
            )
            self._db.commit()

    def update_ticket_status(self, ticket_id, status, order_id=None, exchange_order_id=None):
        with self._lock:
            self._db.execute(
                "UPDATE tickets SET status=?, order_id=COALESCE(?,order_id), "
                "exchange_order_id=COALESCE(?,exchange_order_id) WHERE ticket_id=?",
                (status, order_id, exchange_order_id, ticket_id),
            )
            self._db.commit()

    def get_ticket(self, ticket_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
        if not row:
            return None
        cols = [c[1] for c in self._db.execute("PRAGMA table_info(tickets)").fetchall()]
        return dict(zip(cols, row))

    def save_missed_trade(self, entry):
        ts = entry.get("ts") or int(datetime.now(timezone.utc).timestamp())
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO missed_trades (ts,symbol,side,strategy,reason_code,reason,"
                "price,score,evidence,metadata) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, entry["symbol"], entry.get("side"), entry.get("strategy"),
                    entry.get("reason_code"), entry.get("reason"), entry.get("price"),
                    entry.get("score"), json.dumps(entry.get("evidence") or []),
                    json.dumps(entry.get("metadata") or {}),
                ),
            )
            self._db.commit()
            return cur.lastrowid

    def missed_trades_summary(self, limit=25):
        with self._lock:
            rows = self._db.execute(
                "SELECT reason_code, COUNT(*) as n FROM missed_trades GROUP BY reason_code "
                "ORDER BY n DESC LIMIT ?", (limit,)
            ).fetchall()
            recent = self._db.execute(
                "SELECT ts,symbol,side,reason_code,reason FROM missed_trades "
                "ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return {
            "by_reason_code": [{"reason_code": r[0], "count": r[1]} for r in rows],
            "recent": [
                {"ts": r[0], "symbol": r[1], "side": r[2], "reason_code": r[3], "reason": r[4]}
                for r in recent
            ],
        }

    def save_funnel(self, funnel):
        with self._lock:
            for k, v in funnel.items():
                self._db.execute(
                    "INSERT OR REPLACE INTO telemetry (key,value,updated) VALUES (?,?,?)",
                    (f"funnel:{k}", json.dumps(v), int(datetime.now(timezone.utc).timestamp())),
                )
            self._db.commit()

    def load_funnel(self):
        with self._lock:
            rows = self._db.execute("SELECT key,value FROM telemetry WHERE key LIKE 'funnel:%'").fetchall()
        return {row[0][7:]: json.loads(row[1]) for row in rows}

    def save_trade_journal(self, entry):
        ts = entry.get("closed_at") or int(datetime.now(timezone.utc).timestamp())
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO journal (ts,symbol,side,entry,pnl,pnl_pct,reason,label,ticket_id,misc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, entry.get("symbol"), entry.get("side"), entry.get("entry"),
                    entry.get("pnl"), entry.get("pnl_pct"), entry.get("reason"),
                    entry.get("label"), entry.get("ticket_id"),
                    json.dumps(entry.get("misc") or {}),
                ),
            )
            self._db.commit()
            return cur.lastrowid

    def journal_summary(self, limit=20):
        with self._lock:
            rows = self._db.execute(
                "SELECT symbol,side,label,COUNT(*) as n FROM journal GROUP BY symbol,side,label "
                "ORDER BY n DESC LIMIT ?", (limit,)
            ).fetchall()
            recent = self._db.execute(
                "SELECT symbol,side,label,pnl,pnl_pct,reason FROM journal ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {
            "by_symbol_side_label": [{"symbol": r[0], "side": r[1], "label": r[2], "count": r[3]} for r in rows],
            "recent": [
                {"symbol": r[0], "side": r[1], "label": r[2], "pnl": r[3],
                 "pnl_pct": r[4], "reason": r[5]}
                for r in recent
            ],
        }

    def decision_summary(self, limit=25):
        with self._lock:
            rows = self._db.execute(
                "SELECT status,COUNT(*) as n FROM decisions GROUP BY status"
            ).fetchall()
            by_code = self._db.execute(
                "SELECT reason_code,COUNT(*) as n FROM decisions WHERE reason_code IS NOT NULL "
                "GROUP BY reason_code ORDER BY n DESC LIMIT ?", (limit,)
            ).fetchall()
            recent = self._db.execute(
                "SELECT ts,symbol,status,score,confidence,regime,reason_code FROM decisions "
                "ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return {
            "by_status": dict(rows),
            "by_reason_code": [{"reason_code": r[0], "count": r[1]} for r in by_code],
            "recent": [
                {"ts": r[0], "symbol": r[1], "status": r[2], "score": r[3],
                 "confidence": r[4], "regime": r[5], "reason_code": r[6]}
                for r in recent
            ],
        }
