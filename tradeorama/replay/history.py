"""Past Pinnacle lines from The Odds API's historical endpoints (paid plans only).

- GET /v4/historical/sports/{sport}/odds?date=... returns the snapshot taken at or before `date` (one every
  5 minutes since late 2022) as {"timestamp", "previous_timestamp", "next_timestamp", "data": [events]}.
  It costs 10 credits per market per region, and `bookmakers=pinnacle` counts as one region: 10 credits a call,
  covering every game of the sport at that moment.
- GET /v4/historical/sports/{sport}/events?date=... lists the games without odds, for 1 credit.

The API key goes in the URL, so nothing here logs or prints URLs.
"""

import math
from datetime import datetime, timezone

import httpx

from ..strategy.odds import ODDS_API_HOST

ODDS_CALL_CREDITS = 10
EVENTS_CALL_CREDITS = 1


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_times(commences: list[float], hours: float, step_min: int = 30) -> list[int]:
    """When to ask for snapshots on one game day: every `step_min` minutes from `hours` before the first game
    until the last one starts, plus one just before any start time that isn't on that grid (the closing line)."""
    if not commences:
        return []
    step = step_min * 60
    t = math.floor((min(commences) - hours * 3600) / step) * step
    times = set()
    while t <= max(commences):
        times.add(t)
        t += step
    for c in commences:
        if c % step:
            times.add(int(c) - 60)
    return sorted(times)


def estimated_credits(days: int, hours: float, step_min: int = 30, spread_hours: float = 4) -> int:
    """Rough cost before the schedule is known: games on one day usually start within about 4 hours."""
    per_day = math.ceil((hours + spread_hours) * 60 / step_min) + 1
    return days * (EVENTS_CALL_CREDITS + ODDS_CALL_CREDITS * per_day)


class OddsHistory:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("ODDS_API_KEY is not set (it's read from .env, like the sports bot)")
        self.api_key = api_key
        self.http = client or httpx.Client(timeout=30)
        self.remaining: float | None = None  # credits left this month, from the last response
        self.spent = 0.0  # credits used by this run

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self.http.get(f"{ODDS_API_HOST}{path}", params={"apiKey": self.api_key, **params})
        except httpx.TransportError as exc:
            raise RuntimeError(f"can't reach The Odds API ({type(exc).__name__})") from None
        if r.headers.get("x-requests-remaining"):
            self.remaining = float(r.headers["x-requests-remaining"])
        if r.headers.get("x-requests-last"):
            self.spent += float(r.headers["x-requests-last"])
        if r.status_code != 200:
            body = r.text[:300].replace(self.api_key, "***")
            hint = " Historical odds need a paid plan." if r.status_code in (401, 403) else ""
            raise RuntimeError(f"The Odds API {path} returned {r.status_code}: {body}{hint}")
        return r.json()

    def events(self, sport: str, at: float) -> dict:
        return self._get(f"/historical/sports/{sport}/events", {"date": iso(at)})

    def odds(self, sport: str, at: float) -> dict:
        return self._get(
            f"/historical/sports/{sport}/odds",
            {"date": iso(at), "bookmakers": "pinnacle", "markets": "h2h", "oddsFormat": "decimal"},
        )
