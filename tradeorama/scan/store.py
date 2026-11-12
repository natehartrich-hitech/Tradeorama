"""SQLite cache for the structural scan (scan.db by default), so an interrupted fetch picks up where it stopped."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    ticker TEXT PRIMARY KEY, category TEXT, fee_type TEXT, fee_multiplier REAL, volume REAL,
    status TEXT, events_listed INTEGER, events_in_period INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    event_ticker TEXT PRIMARY KEY, series TEXT, strike_ts REAL, fee_type TEXT, fee_multiplier REAL,
    weight REAL, markets_done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, event_ticker TEXT, ref_ts REAL, open_ts REAL, close_ts REAL,
    settled_ts REAL, result TEXT, weight REAL, candles_done INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS markets_event ON markets(event_ticker);
CREATE TABLE IF NOT EXISTS candles (
    market INTEGER, end_ts INTEGER, bid REAL, ask REAL, low REAL, high REAL, volume REAL,
    PRIMARY KEY (market, end_ts)
) WITHOUT ROWID;
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def commit(self) -> None:
        self.db.commit()

    def save_series(self, rows: list[tuple]) -> None:
        """rows: (ticker, category, fee_type, fee_multiplier, volume). Keeps the fetch status of known series."""
        self.db.executemany(
            "INSERT INTO series (ticker, category, fee_type, fee_multiplier, volume) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET category = excluded.category, fee_type = excluded.fee_type, "
            "fee_multiplier = excluded.fee_multiplier, volume = excluded.volume", rows)
        self.db.commit()

    def series_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM series").fetchall()

    def set_series_status(self, ticker: str, status: str, listed: int | None = None, in_period: int | None = None):
        self.db.execute("UPDATE series SET status = ?, events_listed = COALESCE(?, events_listed), "
                        "events_in_period = COALESCE(?, events_in_period) WHERE ticker = ?",
                        (status, listed, in_period, ticker))
        self.db.commit()

    def save_event(self, e: tuple) -> None:
        """(event_ticker, series, strike_ts, fee_type, fee_multiplier, weight)"""
        self.db.execute("INSERT OR IGNORE INTO events (event_ticker, series, strike_ts, fee_type, fee_multiplier, weight) "
                        "VALUES (?, ?, ?, ?, ?, ?)", e)

    def events_todo(self, series: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM events WHERE series = ? AND markets_done = 0 ORDER BY event_ticker",
                               (series,)).fetchall()

    def save_markets(self, event_ticker: str, rows: list[tuple]) -> None:
        """rows: (ticker, ref_ts, open_ts, close_ts, settled_ts, result, weight)"""
        self.db.executemany(
            "INSERT OR IGNORE INTO markets (ticker, event_ticker, ref_ts, open_ts, close_ts, settled_ts, result, weight) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(r[0], event_ticker, *r[1:]) for r in rows])
        self.db.execute("UPDATE events SET markets_done = 1 WHERE event_ticker = ?", (event_ticker,))
        self.db.commit()

    def markets_todo(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT m.*, e.series FROM markets m JOIN events e USING (event_ticker) WHERE m.candles_done = 0 "
            "ORDER BY m.id").fetchall()

    def save_candles(self, market_id: int, rows: list[tuple]) -> None:
        """rows: (end_ts, bid, ask, low, high, volume)"""
        self.db.executemany("INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [(market_id, *r) for r in rows])
        self.db.execute("UPDATE markets SET candles_done = 1 WHERE id = ?", (market_id,))

    def all_markets(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT m.*, e.series, e.fee_type AS ev_fee_type, e.fee_multiplier AS ev_fee_multiplier, "
            "s.category, s.fee_type AS s_fee_type, s.fee_multiplier AS s_fee_multiplier "
            "FROM markets m JOIN events e USING (event_ticker) JOIN series s ON s.ticker = e.series "
            "WHERE m.candles_done = 1").fetchall()

    def candles_of(self, market_id: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM candles WHERE market = ? ORDER BY end_ts", (market_id,)).fetchall()

    def counts(self) -> dict[str, int]:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "series_done": q("SELECT COUNT(*) FROM series WHERE status IS NOT NULL"),
            "events": q("SELECT COUNT(*) FROM events"),
            "markets": q("SELECT COUNT(*) FROM markets"),
            "markets_with_candles": q("SELECT COUNT(*) FROM markets WHERE candles_done = 1"),
            "candles": q("SELECT COUNT(*) FROM candles"),
        }
