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
        self._db.commit()

    def record_trade(self, symbol, side, entry, exit_px, qty, pnl, pnl_pct, reason, strategy="bot"):
        ts = int(datetime.now(timezone.utc).timestamp())
        with self._lock:
            self._db.execute(
                "INSERT INTO trades (ts,symbol,side,strategy,entry,exit,qty,pnl,pnl_pct,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, symbol, side, strategy, entry, exit_px, qty, pnl, pnl_pct, reason),
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
            self._db.execute(
                "INSERT INTO experience (ts,symbol,side,strategy,setup,outcome,exit_reason,pnl,pnl_pct,lesson) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, symbol, side, strategy, json.dumps(setup), outcome, exit_reason, pnl, pnl_pct, lesson),
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

    def close(self):
        with self._lock:
            self._db.close()
