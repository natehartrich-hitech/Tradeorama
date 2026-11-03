"""Pair Pinnacle games with Polymarket moneyline markets.

A wrong match means trading on the wrong probability, so matching is strict: anything
ambiguous is skipped, and every match is visible in `scan` output before going live.
"""

import re
from dataclasses import dataclass
from typing import Callable
from datetime import datetime, timedelta

import httpx

from ..config import GAMMA_HOST
from ..markets import _json_list
from .odds import Line, parse_time

# The Odds API sport key -> Polymarket Gamma tag slug.
SPORT_TAGS = {
    "americanfootball_nfl": "nfl",
    "americanfootball_ncaaf": "cfb",
    "basketball_nba": "nba",
    "basketball_ncaab": "cbb",
    "basketball_wnba": "wnba",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "soccer_epl": "epl",
    "soccer_uefa_champs_league": "ucl",
    "soccer_spain_la_liga": "la-liga",
    "soccer_italy_serie_a": "serie-a",
    "soccer_germany_bundesliga": "bundesliga",
    "soccer_usa_mls": "mls",
    "mma_mixed_martial_arts": "ufc",
}

# Markets on the same game that are NOT the moneyline. Trading these with moneyline odds would be wrong.
NOT_MONEYLINE = re.compile(
    r"spread|handicap|o/u|over|under|total|points|goals|1h|2h|half|quarter|period|inning|"
    r"by \d|\([+-]\d|[+-]\d+\.5|exact|score|both teams|map|round|method|ko|submission",
    re.I,
)
# Polymarket's game start time must be this close to Pinnacle's. Tight, because baseball and
# hockey teams often play the same opponent on consecutive days.
MAX_START_GAP = timedelta(hours=3)
# One game maps to at most a team-vs-team market or three Yes/No markets (home, away, draw).
MAX_MARKETS_PER_GAME = 3
CLUB_SUFFIXES = {"fc", "cf", "afc", "sc", "ac", "the"}


@dataclass(frozen=True)
class Leg:
    """One Polymarket token to consider buying, with Pinnacle's fair probability for it."""

    token_id: str
    fair: float
    label: str  # e.g. "Lakers" or "Yes: Will Arsenal FC win?"
    market_id: str
    question: str
    line: Line
    fee_rate: float | None = None  # taker fee coefficient, when the venue publishes it per market
    # Best bid/ask for buying this outcome, when the venue's listing includes them. Lets the bot
    # skip the order-book request when the best price already shows no edge.
    quote_bid: float | None = None
    quote_ask: float | None = None


def norm(name: str) -> list[str]:
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", "and")).split()
    return [w for w in words if w not in CLUB_SUFFIXES]


def same_team(poly_name: str, odds_name: str) -> bool:
    """'Lakers' matches 'Los Angeles Lakers'. 'Man City' does not match 'Manchester City': skipped, not guessed."""
    a, b = norm(poly_name), norm(odds_name)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    n = len(shorter)
    # The shorter name must be the longer one's nickname (last words) or, if 2+ words, its city (first words).
    return longer[-n:] == shorter or (n >= 2 and longer[:n] == shorter)


def mentions(text: str, team: str) -> bool:
    words = f" {' '.join(norm(text))} "
    return f" {' '.join(norm(team))} " in words


def parse_start(value: str) -> datetime:
    # Gamma uses both "2026-11-05T00:00:00Z" and "2026-11-05 00:00:00+00".
    value = re.sub(r"([+-]\d\d)$", r"\1:00", value.strip().replace(" ", "T"))
    return parse_time(value)


def market_start(m: dict, event: dict) -> datetime | None:
    # Only real kickoff times count; a market's endDate can be days after the game.
    for value in (m.get("gameStartTime"), event.get("startTime")):
        if value:
            try:
                return parse_start(str(value))
            except ValueError:
                continue
    return None


def legs_for_market(m: dict, line: Line) -> list[tuple[str, float, str]]:
    """Return (token_id, fair, label) for each outcome of `m` that maps unambiguously onto `line`."""
    question = m.get("question") or ""
    if m.get("sportsMarketType") and m["sportsMarketType"] != "moneyline":
        return []
    if NOT_MONEYLINE.search(question):
        return []
    outcomes = _json_list(m.get("outcomes"))
    tokens = _json_list(m.get("clobTokenIds"))
    if len(outcomes) != 2 or len(tokens) != 2:
        return []
    has_draw = "Draw" in line.fair
    teams = [line.home, line.away]

    if [o.lower() for o in outcomes] == ["yes", "no"]:
        # "Will Arsenal FC win on 2026-11-07?" / "Will Arsenal FC vs. Chelsea FC end in a draw?"
        q = question.lower()
        if "draw" in q:
            if not has_draw or not all(mentions(question, t) for t in teams):
                return []
            p = line.fair["Draw"]
        else:
            named = [t for t in teams if mentions(question, t)]
            if len(named) != 1 or "win" not in q:
                return []
            p = line.fair[named[0]]
        return [(tokens[0], p, f"Yes: {question}"), (tokens[1], 1 - p, f"No: {question}")]

    # Team-vs-team market. Only valid when the game cannot end in a draw.
    if has_draw:
        return []
    mapped = []
    for outcome, token in zip(outcomes, tokens):
        hits = [t for t in teams if same_team(outcome, t)]
        if len(hits) != 1:
            return []
        mapped.append((token, line.fair[hits[0]], outcome, hits[0]))
    if mapped[0][3] == mapped[1][3]:
        return []
    return [(token, fair, label) for token, fair, label, _ in mapped]


def match(line: Line, events: list[dict]) -> list[Leg]:
    legs = []
    for event in events:
        for m in event.get("markets") or []:
            if m.get("closed") or m.get("acceptingOrders") is False or m.get("enableOrderBook") is False:
                continue
            start = market_start(m, event)
            if start is None or abs(start - line.commence) > MAX_START_GAP:
                continue
            for token, fair, label in legs_for_market(m, line):
                legs.append(Leg(token, fair, label, str(m.get("id")), m.get("question") or "", line))
    if len({leg.market_id for leg in legs}) > MAX_MARKETS_PER_GAME:
        return []  # ambiguous: more markets claim this game than one game can have
    return legs


def why_unmatched(m: dict, event: dict, line: Line) -> str:
    """Plain-language reason `match` skips market `m` for `line`, or 'matched'. Used by the diagnose command."""
    if m.get("closed"):
        return "market is closed"
    if m.get("acceptingOrders") is False or m.get("enableOrderBook") is False:
        return "market isn't accepting orders"
    start = market_start(m, event)
    if start is None:
        return f"no game start time (gameStartTime={m.get('gameStartTime')!r}, event startTime={event.get('startTime')!r})"
    gap = abs(start - line.commence)
    if gap > MAX_START_GAP:
        return f"starts {start:%m-%d %H:%M} UTC, {gap} from Pinnacle's {line.commence:%m-%d %H:%M}"
    if m.get("sportsMarketType") and m["sportsMarketType"] != "moneyline":
        return f"not a moneyline (sportsMarketType={m['sportsMarketType']!r})"
    question = m.get("question") or ""
    if NOT_MONEYLINE.search(question):
        return "question looks like a spread/total/period market"
    outcomes = _json_list(m.get("outcomes"))
    if len(outcomes) != 2 or len(_json_list(m.get("clobTokenIds"))) != 2:
        return f"expected 2 outcomes, got {outcomes}"
    if legs_for_market(m, line):
        return "matched"
    teams = [line.home, line.away]
    if [o.lower() for o in outcomes] == ["yes", "no"]:
        named = [t for t in teams if mentions(question, t)]
        return f"Yes/No question names {named or 'neither team'} by Pinnacle's names {teams}"
    if "Draw" in line.fair:
        return "team-vs-team market, but Pinnacle's line has a draw"
    return f"outcomes {outcomes} don't map one-to-one onto Pinnacle's teams {teams}"


def candidates(line: Line, events: list[dict]) -> list[tuple[dict, dict]]:
    """Markets that look loosely related to `line`'s game, for diagnosis."""
    keys = {norm(t)[-1] for t in (line.home, line.away) if norm(t)}
    found = []
    for event in events:
        for m in event.get("markets") or []:
            text = " ".join(norm(" ".join([m.get("question") or "", event.get("title") or "", *map(str, _json_list(m.get("outcomes")))])))
            if any(f" {k} " in f" {text} " for k in keys):
                found.append((m, event))
    return found


class SportsMarkets:
    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=15)

    PAGE = 100  # Gamma returns at most 100 events per request, whatever limit is asked for
    MAX_PAGES = 50

    def events(self, sport: str) -> list[dict]:
        tag = SPORT_TAGS.get(sport)
        if not tag:
            raise ValueError(f"no Polymarket tag known for {sport}; add it to SPORT_TAGS")
        # Leagues like the NFL have hundreds of season-long markets (MVP, Super Bowl winner, ...),
        # so the game markets can be several pages in. Gamma requests don't use Odds API credits.
        events: list[dict] = []
        for _ in range(self.MAX_PAGES):
            r = self.http.get(
                f"{GAMMA_HOST}/events",
                params={
                    "tag_slug": tag,
                    "active": "true",
                    "closed": "false",
                    "limit": self.PAGE,
                    "offset": len(events),
                },
            )
            r.raise_for_status()
            batch = r.json()
            events += batch
            if not batch:
                break
        return events


class PolymarketIntl:
    """The international Polymarket site (gamma-api / CLOB) as a venue. Closed to US residents."""

    name = "polymarket"

    def __init__(self, sports, book: Callable[[str], dict], fee_rate: Callable[[str], float]):
        self.sports = sports
        self._book = book
        self._fee_rate = fee_rate

    def events(self, sport: str) -> list[dict]:
        return self.sports.events(sport)

    def match(self, line: Line, events: list[dict]) -> list[Leg]:
        return match(line, events)

    def book(self, token_id: str) -> dict:
        return self._book(token_id)

    def fee_rate(self, leg: Leg) -> float:
        return float(self._fee_rate(leg.token_id) or 0)

    def payout_per_share(self, token_id: str, market_id: str) -> float | None:
        m = self.sports.http.get(f"{GAMMA_HOST}/markets/{market_id}").json()
        if not m.get("closed"):
            return None
        prices = [float(p) for p in _json_list(m.get("outcomePrices"))]
        tokens = _json_list(m.get("clobTokenIds"))
        if token_id not in tokens or not prices or max(prices) < 0.99:
            return None
        return prices[tokens.index(token_id)]
