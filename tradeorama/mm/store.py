"""SQLite store for the market-making test (mm.db). A restart picks up where it stopped."""

import json
import sqlite3
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS programs (
    id TEXT PRIMARY KEY, market TEXT, series TEXT, start_ts REAL, end_ts REAL, reward REAL, target REAL, discount REAL
);
CREATE INDEX IF NOT EXISTS programs_market ON programs(market);
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY, series TEXT, category TEXT, maker_coef REAL, selected_ts REAL, active INTEGER,
    status TEXT, result TEXT, trades_since REAL
);
CREATE TABLE IF NOT EXISTS books (market TEXT, ts REAL, yes TEXT, no TEXT, PRIMARY KEY (market, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS trades (
    market TEXT, ts REAL, id BLOB, yes_price REAL, count REAL, taker_yes INTEGER, PRIMARY KEY (market, ts, id)
) WITHOUT ROWID;
"""


def trade_key(trade_id: str) -> bytes:
    try:
        return uuid.UUID(trade_id).bytes
    except ValueError:
        return trade_id.encode()


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    def save_programs(self, rows: list[tuple]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO programs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def programs(self, market: str | None = None) -> list[sqlite3.Row]:
        if market is None:
            return self.db.execute("SELECT * FROM programs").fetchall()
        return self.db.execute("SELECT * FROM programs WHERE market = ?", (market,)).fetchall()

    def selected(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM markets WHERE active = 1 ORDER BY ticker").fetchall()

    def all_markets(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM markets ORDER BY ticker").fetchall()

    def select(self, ticker: str, series: str, category: str, maker_coef: float, ts: float) -> None:
        self.db.execute(
            "INSERT INTO markets (ticker, series, category, maker_coef, selected_ts, active, trades_since) "
            "VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT(ticker) DO UPDATE SET active = 1",
            (ticker, series, category, maker_coef, ts, ts))

    def drop(self, ticker: str) -> None:
        self.db.execute("UPDATE markets SET active = 0 WHERE ticker = ?", (ticker,))

    def set_status(self, ticker: str, status: str | None, result: str | None) -> None:
        self.db.execute("UPDATE markets SET status = ?, result = ? WHERE ticker = ?", (status, result, ticker))

    def save_book(self, market: str, ts: float, yes, no) -> None:
        """yes/no: (price, size) levels, or "=" for "same as this market's previous snapshot"."""
        enc = lambda v: v if v == "=" else json.dumps(v, separators=(",", ":"))
        self.db.execute("INSERT OR REPLACE INTO books VALUES (?, ?, ?, ?)", (market, ts, enc(yes), enc(no)))

    def save_trades(self, market: str, trades: list[tuple], since: float) -> None:
        self.db.executemany("INSERT OR IGNORE INTO trades VALUES (?, ?, ?, ?, ?, ?)",
                            [(market, ts, trade_key(i), p, c, int(ty)) for ts, i, p, c, ty in trades])
        self.db.execute("UPDATE markets SET trades_since = ? WHERE ticker = ?", (since, market))

    def books(self, market: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM books WHERE market = ? ORDER BY ts", (market,)).fetchall()

    def trades(self, market: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE market = ? ORDER BY ts", (market,)).fetchall()

    def counts(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {"markets": q("SELECT COUNT(*) FROM markets"), "active": q("SELECT COUNT(*) FROM markets WHERE active = 1"),
                "books": q("SELECT COUNT(*) FROM books"), "trades": q("SELECT COUNT(*) FROM trades"),
                "first": q("SELECT MIN(ts) FROM books"), "last": q("SELECT MAX(ts) FROM books")}
