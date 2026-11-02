"""Pinnacle odds via The Odds API (https://the-odds-api.com), with the bookmaker margin removed."""

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

ODDS_API_HOST = "https://api.the-odds-api.com/v4"


@dataclass(frozen=True)
class Line:
    """One game's no-vig moneyline from Pinnacle."""

    event_id: str
    sport: str
    home: str
    away: str
    commence: datetime
    updated: datetime
    fair: dict[str, float]  # outcome name ("Draw" for soccer) -> probability, summing to 1


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def devig(decimal_odds: dict[str, float]) -> dict[str, float]:
    """Remove the margin by scaling implied probabilities to sum to 1 (multiplicative method)."""
    implied = {name: 1 / price for name, price in decimal_odds.items() if price > 1}
    if len(implied) != len(decimal_odds):
        return {}
    total = sum(implied.values())
    return {name: p / total for name, p in implied.items()}


def parse_events(events: list[dict], bookmaker: str = "pinnacle") -> list[Line]:
    lines = []
    for ev in events:
        book = next((b for b in ev.get("bookmakers") or [] if b.get("key") == bookmaker), None)
        market = next((m for m in (book or {}).get("markets") or [] if m.get("key") == "h2h"), None)
        if not market:
            continue
        fair = devig({o["name"]: float(o["price"]) for o in market.get("outcomes") or []})
        if not fair:
            continue
        lines.append(
            Line(
                event_id=ev["id"],
                sport=ev["sport_key"],
                home=ev["home_team"],
                away=ev["away_team"],
                commence=parse_time(ev["commence_time"]),
                updated=parse_time(market.get("last_update") or book["last_update"]),
                fair=fair,
            )
        )
    return lines


class PinnacleOdds:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("ODDS_API_KEY is not set")
        self.api_key = api_key
        self.http = client or httpx.Client(timeout=15)
        self.credits_remaining: str | None = None

    def lines(self, sport: str) -> list[Line]:
        r = self.http.get(
            f"{ODDS_API_HOST}/sports/{sport}/odds",
            params={
                "apiKey": self.api_key,
                "bookmakers": "pinnacle",
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
        )
        r.raise_for_status()
        self.credits_remaining = r.headers.get("x-requests-remaining")
        return parse_events(r.json())
