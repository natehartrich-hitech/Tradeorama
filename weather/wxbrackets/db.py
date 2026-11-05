"""SQLite storage: every forecast run, its member maxima and bracket probabilities, and official outcomes."""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_runs (
    run_id INTEGER PRIMARY KEY,
    created_utc TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'high',  -- 'high' or 'low': which daily extreme was forecast
    station TEXT NOT NULL,
    target_date TEXT NOT NULL,
    lead_hours REAL NOT NULL,          -- hours from run to the start of the climate day (negative = same day)
    bias_f REAL NOT NULL,
    spread_inflation REAL NOT NULL,
    bandwidth_f REAL NOT NULL,
    pooled_mean_f REAL NOT NULL,       -- weighted mean of raw member values (maxima or minima), before bias/inflation
    dist_mean_f REAL NOT NULL,         -- mean of the final distribution (after bias and any conditioning)
    observed_max_f REAL,               -- same-day conditioning value, if used (observed min for lows)
    nws_forecast_high_f REAL
);
CREATE TABLE IF NOT EXISTS forecast_members (
    run_id INTEGER NOT NULL REFERENCES forecast_runs(run_id),
    created_utc TEXT NOT NULL,
    station TEXT NOT NULL,
    target_date TEXT NOT NULL,
    model TEXT NOT NULL,
    member INTEGER NOT NULL,
    member_max_f REAL NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (run_id, model, member)
);
CREATE TABLE IF NOT EXISTS bracket_probs (
    run_id INTEGER NOT NULL REFERENCES forecast_runs(run_id),
    idx INTEGER NOT NULL,
    label TEXT NOT NULL,
    low_f INTEGER,                     -- reported °F range, inclusive; NULL = open-ended
    high_f INTEGER,
    prob REAL NOT NULL,
    PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS grib_points (       -- cache: one model value per GRIB file and location
    url TEXT NOT NULL,
    field TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    value_f REAL NOT NULL,
    PRIMARY KEY (url, field, lat, lon)
);
CREATE TABLE IF NOT EXISTS market_prices (     -- Polymarket US quotes captured with each forecast run
    run_id INTEGER NOT NULL REFERENCES forecast_runs(run_id),
    slug TEXT NOT NULL,
    label TEXT NOT NULL,
    low_f INTEGER,
    high_f INTEGER,
    yes_bid REAL,
    yes_ask REAL,
    fee_rate REAL NOT NULL,
    PRIMARY KEY (run_id, slug)
);
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY,
    created_utc TEXT NOT NULL,
    run_id INTEGER REFERENCES forecast_runs(run_id),
    station TEXT NOT NULL,
    target_date TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'high',
    slug TEXT NOT NULL,
    side TEXT NOT NULL,                -- 'yes' or 'no'
    label TEXT NOT NULL,               -- bracket, e.g. "64-65"
    contracts REAL NOT NULL,
    cost REAL NOT NULL,                -- USD, fees included
    prob REAL NOT NULL,                -- model probability of this side winning
    edge REAL NOT NULL,                -- expected profit per contract after fees
    payout REAL                        -- USD at settlement; NULL until settled
);
CREATE TABLE IF NOT EXISTS outcomes (
    station TEXT NOT NULL,
    climate_date TEXT NOT NULL,
    max_f INTEGER NOT NULL,
    final INTEGER NOT NULL,            -- 0 if only a same-day (partial) report was found
    min_f INTEGER,                     -- the day's official low (NULL if not reported)
    source TEXT NOT NULL,
    fetched_utc TEXT NOT NULL,
    PRIMARY KEY (station, climate_date)
);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (lows: Nov 2026)."""
        for table, column, ddl in (
            ("forecast_runs", "kind", "TEXT NOT NULL DEFAULT 'high'"),
            ("paper_fills", "kind", "TEXT NOT NULL DEFAULT 'high'"),
            ("outcomes", "min_f", "INTEGER"),
        ):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self.conn.commit()

    def save_run(self, run: dict, members: list[tuple[str, int, float, float]], brackets: list[tuple]) -> int:
        """members: (model, member, max_f, weight); brackets: (label, low, high, prob)."""
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO forecast_runs ({', '.join(run)}) VALUES ({', '.join('?' * len(run))})",
                list(run.values()),
            )
            run_id = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO forecast_members VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, run["created_utc"], run["station"], run["target_date"], m, n, v, w) for m, n, v, w in members],
            )
            self.conn.executemany(
                "INSERT INTO bracket_probs VALUES (?, ?, ?, ?, ?, ?)",
                [(run_id, i, *b) for i, b in enumerate(brackets)],
            )
        return run_id

    def save_outcome(self, station: str, day: date, max_f: int, final: bool, source: str, min_f: int | None = None) -> None:
        with self.conn:
            # Never let a partial same-day value overwrite a final one.
            self.conn.execute(
                """INSERT INTO outcomes (station, climate_date, max_f, final, source, fetched_utc, min_f)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(station, climate_date) DO UPDATE SET
                     max_f = excluded.max_f, final = excluded.final, min_f = excluded.min_f,
                     source = excluded.source, fetched_utc = excluded.fetched_utc
                   WHERE excluded.final >= outcomes.final""",
                (station, day.isoformat(), max_f, int(final), source, now_utc(), min_f),
            )

    def scored_runs(self, station: str, min_lead: float | None = None, max_lead: float | None = None,
                    kind: str = "high") -> list[sqlite3.Row]:
        official = "o.max_f" if kind == "high" else "o.min_f"
        sql = f"""SELECT r.*, {official} AS official_f FROM forecast_runs r
                 JOIN outcomes o ON o.station = r.station AND o.climate_date = r.target_date AND o.final = 1
                 WHERE r.station = ? AND r.kind = ? AND {official} IS NOT NULL"""
        args: list = [station, kind]
        if min_lead is not None:
            sql += " AND r.lead_hours >= ?"
            args.append(min_lead)
        if max_lead is not None:
            sql += " AND r.lead_hours <= ?"
            args.append(max_lead)
        return self.conn.execute(sql + " ORDER BY r.target_date, r.created_utc", args).fetchall()

    def member_errors(self, station: str, min_lead: float | None = None, max_lead: float | None = None,
                      kind: str = "high") -> list[sqlite3.Row]:
        """Each model's mean member value per scored run, with the official high (or low)."""
        official = "o.max_f" if kind == "high" else "o.min_f"
        sql = f"""SELECT m.model, m.run_id, r.lead_hours, AVG(m.member_max_f) AS mean_max_f, COUNT(*) AS members,
                        {official} AS official_f
                 FROM forecast_members m
                 JOIN forecast_runs r ON r.run_id = m.run_id
                 JOIN outcomes o ON o.station = m.station AND o.climate_date = m.target_date AND o.final = 1
                 WHERE m.station = ? AND r.kind = ? AND {official} IS NOT NULL"""
        args: list = [station, kind]
        if min_lead is not None:
            sql += " AND r.lead_hours >= ?"
            args.append(min_lead)
        if max_lead is not None:
            sql += " AND r.lead_hours <= ?"
            args.append(max_lead)
        return self.conn.execute(sql + " GROUP BY m.model, m.run_id", args).fetchall()

    def save_market_prices(self, run_id: int, rows: list[tuple]) -> None:
        """rows: (slug, label, low_f, high_f, yes_bid, yes_ask, fee_rate)"""
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO market_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                  [(run_id, *r) for r in rows])

    def market_prices(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM market_prices WHERE run_id = ?", (run_id,)).fetchall()

    def record_paper_fill(self, **f) -> None:
        f.setdefault("created_utc", now_utc())
        with self.conn:
            self.conn.execute(f"INSERT INTO paper_fills ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})", list(f.values()))

    def paper_spent(self, station: str | None = None, target_date: str | None = None, since_utc: str | None = None,
                    kind: str | None = None) -> float:
        sql, args = "SELECT COALESCE(SUM(cost), 0) FROM paper_fills WHERE 1 = 1", []
        for col, val, op in (("station", station, "="), ("target_date", target_date, "="),
                             ("created_utc", since_utc, ">="), ("kind", kind, "=")):
            if val is not None:
                sql += f" AND {col} {op} ?"
                args.append(val)
        return self.conn.execute(sql, args).fetchone()[0]

    def paper_position(self, slug: str, side: str) -> float:
        """USD already paid for this bracket and side (open positions only)."""
        return self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM paper_fills WHERE slug = ? AND side = ? AND payout IS NULL", (slug, side)
        ).fetchone()[0]

    def unsettled_paper(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM paper_fills WHERE payout IS NULL").fetchall()

    def settle_paper(self, fill_id: int, payout: float) -> None:
        with self.conn:
            self.conn.execute("UPDATE paper_fills SET payout = ? WHERE id = ?", (payout, fill_id))

    def paper_fills(self, station: str | None = None, kind: str | None = None) -> list[sqlite3.Row]:
        conds, args = [], []
        for col, val in (("station", station), ("kind", kind)):
            if val:
                conds.append(f"{col} = ?")
                args.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return self.conn.execute(f"SELECT * FROM paper_fills{where} ORDER BY created_utc DESC", args).fetchall()

    def get_point(self, url: str, field: str, lat: float, lon: float) -> float | None:
        row = self.conn.execute(
            "SELECT value_f FROM grib_points WHERE url = ? AND field = ? AND lat = ? AND lon = ?", (url, field, lat, lon)
        ).fetchone()
        return row[0] if row else None

    def put_point(self, url: str, field: str, lat: float, lon: float, value_f: float) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO grib_points VALUES (?, ?, ?, ?, ?)", (url, field, lat, lon, value_f))

    def brackets_for(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM bracket_probs WHERE run_id = ? ORDER BY idx", (run_id,)).fetchall()
