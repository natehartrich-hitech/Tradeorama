"""SQLite record of every trade the bot makes, plus the data needed to judge whether its edges are real."""

import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    venue TEXT NOT NULL DEFAULT 'polymarket',  -- 'polymarket-us' or 'polymarket' (international)
    mode TEXT NOT NULL,             -- 'paper' or 'live'
    sport TEXT NOT NULL,
    event_id TEXT NOT NULL,         -- Pinnacle game id; exposure limits are per game
    commence REAL NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    label TEXT NOT NULL,
    question TEXT NOT NULL,
    size REAL NOT NULL,             -- shares
    cost REAL NOT NULL,             -- USDC, fees included
    fair REAL NOT NULL,             -- Pinnacle no-vig probability at entry
    edge REAL NOT NULL,             -- expected profit per share at entry
    order_id TEXT,
    payout REAL                     -- USDC paid at resolution; NULL until settled
);
-- Latest fair probability seen before each game started: the closing line.
CREATE TABLE IF NOT EXISTS closing (
    token_id TEXT PRIMARY KEY,
    fair REAL NOT NULL,
    ts REAL NOT NULL
);
"""


def day_start(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Ledger:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(fills)")}
        if "venue" not in cols:  # databases from before Polymarket US: those trades were international
            self.db.execute("ALTER TABLE fills ADD COLUMN venue TEXT NOT NULL DEFAULT 'polymarket'")
            self.db.commit()

    def record_fill(self, **f) -> None:
        f.setdefault("ts", time.time())
        cols = ", ".join(f)
        self.db.execute(f"INSERT INTO fills ({cols}) VALUES ({', '.join('?' * len(f))})", list(f.values()))
        self.db.commit()

    def record_closing(self, token_id: str, fair: float, commence: float, now: float | None = None) -> None:
        now = now or time.time()
        if now >= commence:
            return
        self.db.execute(
            "INSERT INTO closing (token_id, fair, ts) VALUES (?, ?, ?) "
            "ON CONFLICT(token_id) DO UPDATE SET fair = excluded.fair, ts = excluded.ts",
            (token_id, fair, now),
        )
        self.db.commit()

    def game_exposure(self, mode: str, event_id: str, venue: str = "polymarket-us") -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM fills WHERE mode = ? AND event_id = ? AND venue = ?",
            (mode, event_id, venue),
        ).fetchone()
        return row[0]

    def position_cost(self, mode: str, token_id: str, venue: str = "polymarket-us") -> float:
        """USD already paid for this outcome (open positions only)."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM fills WHERE mode = ? AND token_id = ? AND venue = ? AND payout IS NULL",
            (mode, token_id, venue),
        ).fetchone()
        return row[0]

    def spent_today(self, mode: str, now: float | None = None, venue: str = "polymarket-us") -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM fills WHERE mode = ? AND ts >= ? AND venue = ?",
            (mode, day_start(now or time.time()), venue),
        ).fetchone()
        return row[0]

    def unsettled(self, now: float | None = None, venue: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM fills WHERE payout IS NULL AND commence < ?", [now or time.time()]
        if venue:
            sql += " AND venue = ?"
            args.append(venue)
        return self.db.execute(sql, args).fetchall()

    def settle(self, fill_id: int, payout: float) -> None:
        self.db.execute("UPDATE fills SET payout = ? WHERE id = ?", (payout, fill_id))
        self.db.commit()

    def trades(self, mode: str | None = None, limit: int = 100, now: float | None = None, venue: str | None = None) -> list[dict]:
        sql = """
            SELECT f.*, c.fair AS closing_fair
            FROM fills f LEFT JOIN closing c ON c.token_id = f.token_id
            {where} ORDER BY f.ts DESC LIMIT ?
        """
        conds, args = [], []
        if mode:
            conds.append("f.mode = ?")
            args.append(mode)
        if venue:
            conds.append("f.venue = ?")
            args.append(venue)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        rows = self.db.execute(sql.format(where=where), [*args, limit]).fetchall()
        now = now or time.time()
        out = []
        for r in rows:
            t = dict(r)
            avg = t["cost"] / t["size"]
            # Closing line value: how much the line moved our way by kickoff. Only meaningful once the game starts.
            t["clv"] = (t["closing_fair"] - avg) if t["closing_fair"] is not None and now >= t["commence"] else None
            t["pnl"] = (t["payout"] - t["cost"]) if t["payout"] is not None else None
            out.append(t)
        return out

    def summary(self, mode: str | None = None, venue: str | None = None) -> dict:
        return summarize(self.trades(mode, limit=100_000, venue=venue))

    def summary_by_sport(self, mode: str, venue: str | None = None) -> dict[str, dict]:
        """Per-sport results, so sports where Pinnacle's edge doesn't hold up can be dropped."""
        groups: dict[str, list[dict]] = {}
        for t in self.trades(mode, limit=100_000, venue=venue):
            groups.setdefault(t["sport"], []).append(t)
        return {sport: summarize(trades) for sport, trades in sorted(groups.items())}


def summarize(trades: list[dict]) -> dict:
    shares = sum(t["size"] for t in trades)
    with_clv = [t for t in trades if t["clv"] is not None]
    settled = [t for t in trades if t["pnl"] is not None]
    settled_staked = sum(t["cost"] for t in settled)
    settled_pnl = sum(t["pnl"] for t in settled)
    return {
        "trades": len(trades),
        "staked_usd": round(sum(t["cost"] for t in trades), 2),
        "open_usd": round(sum(t["cost"] for t in trades if t["payout"] is None), 2),
        "expected_profit_usd": round(sum(t["edge"] * t["size"] for t in trades), 2),
        "avg_edge_cents": round(100 * sum(t["edge"] * t["size"] for t in trades) / shares, 2) if shares else None,
        "clv_trades": len(with_clv),
        "avg_clv_cents": (
            round(100 * sum(t["clv"] * t["size"] for t in with_clv) / sum(t["size"] for t in with_clv), 2)
            if with_clv
            else None
        ),
        "settled": len(settled),
        "settled_pnl_usd": round(settled_pnl, 2),
        "settled_staked_usd": round(settled_staked, 2),
        "roi_pct": round(100 * settled_pnl / settled_staked, 1) if settled_staked else None,
    }
