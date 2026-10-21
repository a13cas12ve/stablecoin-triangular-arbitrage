"""Persistencia en SQLite: evolución de la cuenta, trades, muestras de mercado y eventos."""

import json
import sqlite3
import time

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS equity (
    ts REAL PRIMARY KEY, total_usdt REAL NOT NULL, balances TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
    route TEXT NOT NULL, source TEXT NOT NULL,
    amount_in REAL, expected_out REAL, realized_out REAL,
    theo_bps REAL, expected_bps REAL, realized_bps REAL,
    pnl_usdt REAL, duration_ms REAL, legs TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS market_minutes (
    ts REAL PRIMARY KEY, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, level TEXT NOT NULL, msg TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path=DB_PATH):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def add_equity(self, total_usdt, balances, ts=None):
        self.db.execute("INSERT OR REPLACE INTO equity VALUES (?, ?, ?)",
                        (ts or time.time(), total_usdt, json.dumps(balances)))
        self.db.commit()

    def equity(self, since=0.0):
        rows = self.db.execute("SELECT ts, total_usdt, balances FROM equity WHERE ts >= ? ORDER BY ts",
                               (since,)).fetchall()
        return [{"ts": r["ts"], "total_usdt": r["total_usdt"], "balances": json.loads(r["balances"])}
                for r in rows]

    def first_equity(self):
        row = self.db.execute("SELECT ts, total_usdt FROM equity ORDER BY ts LIMIT 1").fetchone()
        return dict(row) if row else None

    def add_trade(self, t):
        cur = self.db.execute(
            """INSERT INTO trades (ts, mode, status, route, source, amount_in, expected_out, realized_out,
               theo_bps, expected_bps, realized_bps, pnl_usdt, duration_ms, legs, note)
               VALUES (:ts, :mode, :status, :route, :source, :amount_in, :expected_out, :realized_out,
               :theo_bps, :expected_bps, :realized_bps, :pnl_usdt, :duration_ms, :legs, :note)""",
            {**t, "legs": json.dumps(t.get("legs", []))})
        self.db.commit()
        return cur.lastrowid

    def trades(self, limit=1000, mode=None):
        q = "SELECT * FROM trades" + (" WHERE mode = ?" if mode else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.db.execute(q, ((mode, limit) if mode else (limit,))).fetchall()
        return [{**dict(r), "legs": json.loads(r["legs"] or "[]")} for r in rows]

    def trade_stats(self, mode):
        rows = self.db.execute(
            "SELECT realized_bps, pnl_usdt, status FROM trades WHERE mode = ?", (mode,)).fetchall()
        done = [r for r in rows if r["status"] == "completado"]
        pnls = [r["pnl_usdt"] or 0.0 for r in done]
        gains = sum(p for p in pnls if p > 0)
        losses = -sum(p for p in pnls if p < 0)
        return {
            "count": len(rows),
            "completed": len(done),
            "failed": len(rows) - len(done),
            "pnl_usdt": sum(pnls),
            "win_rate": (sum(1 for p in pnls if p > 0) / len(pnls) * 100) if pnls else None,
            "avg_bps": (sum(r["realized_bps"] or 0 for r in done) / len(done)) if done else None,
            "profit_factor": (gains / losses) if losses > 0 else None,
        }

    def add_market_minute(self, ts, data):
        self.db.execute("INSERT OR REPLACE INTO market_minutes VALUES (?, ?)", (ts, json.dumps(data)))
        self.db.commit()

    def market_minutes(self, since):
        rows = self.db.execute("SELECT ts, data FROM market_minutes WHERE ts >= ? ORDER BY ts",
                               (since,)).fetchall()
        return [{"ts": r["ts"], **json.loads(r["data"])} for r in rows]

    def add_event(self, level, msg):
        self.db.execute("INSERT INTO events (ts, level, msg) VALUES (?, ?, ?)", (time.time(), level, msg))
        self.db.commit()

    def events(self, limit=200):
        rows = self.db.execute("SELECT ts, level, msg FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
