"""SQLite cache for the replay, so a rerun never pays for the same Odds API snapshot twice.

Kept apart from the bot's tradeorama.db. Odds API responses are stored as returned (a few MB for a month of
NBA snapshots). Kalshi trades keep only the columns the analysis uses: a month of NBA pregame trades
(about 640,000) takes roughly 40 MB.
"""

import json
import sqlite3
import uuid

from .kalshi import KalshiTrade, parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kalshi_series (ticker TEXT PRIMARY KEY, fee_type TEXT, fee_multiplier REAL);
CREATE TABLE IF NOT EXISTS kalshi_markets (
    id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, event_ticker TEXT, series TEXT, code TEXT, title TEXT,
    result TEXT, close_ts REAL, fee_waiver_until REAL
);
CREATE INDEX IF NOT EXISTS kalshi_markets_event ON kalshi_markets(event_ticker);
-- Clustered by market and time, with 16-byte trade ids: about a third of the size of a plain table with indexes.
CREATE TABLE IF NOT EXISTS kalshi_trades (
    market INTEGER, ts REAL, trade_id BLOB, yes_price REAL, contracts REAL, taker_yes INTEGER, block INTEGER,
    PRIMARY KEY (market, ts, trade_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kalshi_fetched (ticker TEXT, min_ts INTEGER, max_ts INTEGER, PRIMARY KEY (ticker, min_ts, max_ts));
CREATE TABLE IF NOT EXISTS odds_events (sport TEXT, day TEXT, body TEXT, PRIMARY KEY (sport, day));
CREATE TABLE IF NOT EXISTS odds_snapshots (
    sport TEXT, requested_ts INTEGER, snapshot_ts REAL, body TEXT, PRIMARY KEY (sport, requested_ts)
);
CREATE TABLE IF NOT EXISTS games (
    event_ticker TEXT PRIMARY KEY, sport TEXT, day TEXT, odds_event_id TEXT, commence REAL,
    home TEXT, away TEXT, home_code TEXT, away_code TEXT
);
"""


def trade_key(trade_id: str) -> bytes:
    """Kalshi trade ids are UUIDs: 16 bytes instead of 36 characters."""
    try:
        return uuid.UUID(trade_id).bytes
    except ValueError:
        return trade_id.encode()


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def commit(self) -> None:
        self.db.commit()

    # --- Kalshi ---

    def save_series(self, s: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO kalshi_series VALUES (?, ?, ?)",
                        (s["ticker"], s.get("fee_type"), s.get("fee_multiplier")))

    def fee_multiplier(self, series: str) -> float | None:
        row = self.db.execute("SELECT fee_multiplier FROM kalshi_series WHERE ticker = ?", (series,)).fetchone()
        return None if row is None or row[0] is None else float(row[0])

    def save_market(self, series: str, m: dict) -> None:
        waiver = m.get("fee_waiver_expiration_time")
        values = (m["ticker"], m["event_ticker"], series, m["ticker"].rsplit("-", 1)[-1], m.get("title"), m.get("result"),
                  parse_time(m["close_time"]) if m.get("close_time") else None, parse_time(waiver) if waiver else None)
        # An upsert, not INSERT OR REPLACE, so the market keeps the id its trades point to.
        self.db.execute(
            "INSERT INTO kalshi_markets (ticker, event_ticker, series, code, title, result, close_ts, fee_waiver_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET event_ticker = excluded.event_ticker, "
            "series = excluded.series, code = excluded.code, title = excluded.title, result = excluded.result, "
            "close_ts = excluded.close_ts, fee_waiver_until = excluded.fee_waiver_until",
            values,
        )

    def events(self, series: str) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = {}
        for r in self.db.execute("SELECT * FROM kalshi_markets WHERE series = ? ORDER BY ticker", (series,)):
            out.setdefault(r["event_ticker"], []).append(r)
        return out

    def markets_of(self, event_ticker: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM kalshi_markets WHERE event_ticker = ?", (event_ticker,)).fetchall()

    def fetched(self, ticker: str, min_ts: int, max_ts: int) -> bool:
        return self.db.execute("SELECT 1 FROM kalshi_fetched WHERE ticker = ? AND min_ts = ? AND max_ts = ?",
                               (ticker, min_ts, max_ts)).fetchone() is not None

    def market_id(self, ticker: str) -> int:
        row = self.db.execute("SELECT id FROM kalshi_markets WHERE ticker = ?", (ticker,)).fetchone()
        if row is None:
            raise ValueError(f"market {ticker} isn't in the cache")
        return row[0]

    def save_trades(self, ticker: str, min_ts: int, max_ts: int, trades: list[KalshiTrade]) -> None:
        market = self.market_id(ticker)
        self.db.executemany(
            "INSERT OR REPLACE INTO kalshi_trades VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(market, t.ts, trade_key(t.trade_id), t.yes_price, t.contracts, int(t.taker_yes), int(t.block))
             for t in trades],
        )
        self.db.execute("INSERT OR REPLACE INTO kalshi_fetched VALUES (?, ?, ?)", (ticker, min_ts, max_ts))
        self.db.commit()

    def trades(self, ticker: str, min_ts: float, max_ts: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT ts, yes_price, contracts, taker_yes, block FROM kalshi_trades "
            "WHERE market = ? AND ts >= ? AND ts < ? ORDER BY ts", (self.market_id(ticker), min_ts, max_ts)
        ).fetchall()

    # --- The Odds API ---

    def odds_events(self, sport: str, day: str) -> dict | None:
        row = self.db.execute("SELECT body FROM odds_events WHERE sport = ? AND day = ?", (sport, day)).fetchone()
        return None if row is None else json.loads(row[0])

    def save_odds_events(self, sport: str, day: str, body: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO odds_events VALUES (?, ?, ?)", (sport, day, json.dumps(body)))
        self.db.commit()

    def has_snapshot(self, sport: str, requested_ts: int) -> bool:
        return self.db.execute("SELECT 1 FROM odds_snapshots WHERE sport = ? AND requested_ts = ?",
                               (sport, requested_ts)).fetchone() is not None

    def save_snapshot(self, sport: str, requested_ts: int, body: dict) -> None:
        stamp = body.get("timestamp")
        self.db.execute(
            "INSERT OR REPLACE INTO odds_snapshots VALUES (?, ?, ?, ?)",
            (sport, requested_ts, parse_time(stamp) if stamp else requested_ts, json.dumps(body.get("data") or [])),
        )
        self.db.commit()

    def snapshots(self, sport: str, min_ts: float, max_ts: float) -> list[tuple[float, list[dict]]]:
        rows = self.db.execute(
            "SELECT snapshot_ts, body FROM odds_snapshots WHERE sport = ? AND snapshot_ts >= ? AND snapshot_ts < ? "
            "ORDER BY snapshot_ts", (sport, min_ts, max_ts)).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    # --- matched games ---

    def save_game(self, sport: str, day: str, event_ticker: str, odds_game: dict, mapping: dict[str, str]) -> None:
        home, away = odds_game["home_team"], odds_game["away_team"]
        code = {team: c for c, team in mapping.items()}
        self.db.execute(
            "INSERT OR REPLACE INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_ticker, sport, day, odds_game["id"], odds_game["commence_ts"], home, away, code[home], code[away]),
        )

    def games(self, sport: str, first_day: str, last_day: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM games WHERE sport = ? AND day >= ? AND day <= ? ORDER BY commence",
            (sport, first_day, last_day)).fetchall()
