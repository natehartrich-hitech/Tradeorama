"""SQLite storage: every forecast run, its member maxima and bracket probabilities, and official outcomes."""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_runs (
    run_id INTEGER PRIMARY KEY,
    created_utc TEXT NOT NULL,
    station TEXT NOT NULL,
    target_date TEXT NOT NULL,
    lead_hours REAL NOT NULL,          -- hours from run to the start of the climate day (negative = same day)
    bias_f REAL NOT NULL,
    spread_inflation REAL NOT NULL,
    bandwidth_f REAL NOT NULL,
    pooled_mean_f REAL NOT NULL,       -- weighted mean of raw member maxima, before bias/inflation
    dist_mean_f REAL NOT NULL,         -- mean of the final distribution (after bias and any conditioning)
    observed_max_f REAL,               -- same-day conditioning value, if used
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
CREATE TABLE IF NOT EXISTS outcomes (
    station TEXT NOT NULL,
    climate_date TEXT NOT NULL,
    max_f INTEGER NOT NULL,
    final INTEGER NOT NULL,            -- 0 if only a same-day (partial) report was found
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

    def save_outcome(self, station: str, day: date, max_f: int, final: bool, source: str) -> None:
        with self.conn:
            # Never let a partial same-day value overwrite a final one.
            self.conn.execute(
                """INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(station, climate_date) DO UPDATE SET
                     max_f = excluded.max_f, final = excluded.final,
                     source = excluded.source, fetched_utc = excluded.fetched_utc
                   WHERE excluded.final >= outcomes.final""",
                (station, day.isoformat(), max_f, int(final), source, now_utc()),
            )

    def scored_runs(self, station: str, min_lead: float | None = None, max_lead: float | None = None) -> list[sqlite3.Row]:
        sql = """SELECT r.*, o.max_f AS official_f FROM forecast_runs r
                 JOIN outcomes o ON o.station = r.station AND o.climate_date = r.target_date AND o.final = 1
                 WHERE r.station = ?"""
        args: list = [station]
        if min_lead is not None:
            sql += " AND r.lead_hours >= ?"
            args.append(min_lead)
        if max_lead is not None:
            sql += " AND r.lead_hours <= ?"
            args.append(max_lead)
        return self.conn.execute(sql + " ORDER BY r.target_date, r.created_utc", args).fetchall()

    def member_errors(self, station: str, min_lead: float | None = None, max_lead: float | None = None) -> list[sqlite3.Row]:
        """Each model's mean member max per scored run, with the official high."""
        sql = """SELECT m.model, m.run_id, AVG(m.member_max_f) AS mean_max_f, COUNT(*) AS members, o.max_f AS official_f
                 FROM forecast_members m
                 JOIN forecast_runs r ON r.run_id = m.run_id
                 JOIN outcomes o ON o.station = m.station AND o.climate_date = m.target_date AND o.final = 1
                 WHERE m.station = ?"""
        args: list = [station]
        if min_lead is not None:
            sql += " AND r.lead_hours >= ?"
            args.append(min_lead)
        if max_lead is not None:
            sql += " AND r.lead_hours <= ?"
            args.append(max_lead)
        return self.conn.execute(sql + " GROUP BY m.model, m.run_id", args).fetchall()

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
