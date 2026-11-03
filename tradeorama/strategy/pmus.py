"""Polymarket US (polymarket.us), the CFTC-regulated US exchange, as a venue for the sports bot.

Public market data comes from https://gateway.polymarket.us with no API key
(docs: https://docs.polymarket.us/api-reference/introduction). Each market is one Yes/No
instrument with a single order book:
  - moneyline ("76ers vs Pistons"): the long side is one team, the short side the other.
    Buying the short team = selling the long side at its bid, i.e. paying 1 - bid.
  - soccer ("drawable_outcome"): three markets per game - home win, draw, away win -
    each a plain Yes/No, settled on regulation time.
A market's outcomePrices once resolved are [long/Yes payout, short/No payout].
"""

import logging
import random
import time

import httpx

from ..markets import _json_list
from .matching import MAX_START_GAP, Leg, mentions, parse_start, same_team
from .odds import Line

log = logging.getLogger(__name__)

GATEWAY = "https://gateway.polymarket.us"

# The Odds API sport key -> Polymarket US league slug.
LEAGUES = {
    "basketball_nba": "nba",
    "basketball_ncaab": "cbb",
    "basketball_wnba": "wnba",
    "americanfootball_nfl": "nfl",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "soccer_epl": "epl",
    "soccer_uefa_champs_league": "ucl",
    "soccer_spain_la_liga": "lal",
    "soccer_italy_serie_a": "sea",
    "soccer_germany_bundesliga": "bun",
    "soccer_usa_mls": "mls",
    "mma_mixed_martial_arts": "ufc",
}


def token(slug: str, side: str) -> str:
    return f"{slug}:{side}"


def split_token(token_id: str) -> tuple[str, str]:
    slug, _, side = token_id.rpartition(":")
    return slug, side


def team_names(side: dict) -> list[str]:
    team = side.get("team") or {}
    names = [team.get(k) for k in ("name", "alias", "safeName")] + [side.get("description")]
    return [n for n in names if n]


def side_team(side: dict, line: Line) -> str | None:
    """The Pinnacle team this market side refers to, or None if none or both match."""
    hits = {t for t in (line.home, line.away) for n in team_names(side) if same_team(n, t)}
    return hits.pop() if len(hits) == 1 else None


def market_start(m: dict, event: dict):
    for value in (m.get("gameStartTime"), event.get("startTime"), event.get("startDate")):
        if value:
            try:
                return parse_start(str(value))
            except ValueError:
                continue
    return None


def quote(q) -> float | None:
    return float(q["value"]) if q and q.get("value") not in (None, "") else None


def side_quotes(m: dict) -> dict[str, dict]:
    """Listing quotes for buying each side: Yes at the ask; No at 1 - the Yes bid."""
    bid, ask = quote(m.get("bestBidQuote")), quote(m.get("bestAskQuote"))
    flip = lambda x: None if x is None else round(1 - x, 4)  # noqa: E731
    return {"yes": {"quote_bid": bid, "quote_ask": ask}, "no": {"quote_bid": flip(ask), "quote_ask": flip(bid)}}


def legs_for_market(m: dict, event: dict, line: Line) -> list[Leg]:
    if m.get("closed") or m.get("status") not in (None, "MARKET_STATUS_OPEN"):
        return []
    start = market_start(m, event)
    if start is None or abs(start - line.commence) > MAX_START_GAP:
        return []
    slug = m["slug"]
    fee = m.get("feeCoefficient")
    sides = m.get("marketSides") or []
    long_side = next((s for s in sides if s.get("long")), None)
    short_side = next((s for s in sides if s.get("long") is False), None)
    common = dict(market_id=slug, question=m.get("question") or "", line=line, fee_rate=fee)
    q = side_quotes(m)
    has_draw = "Draw" in line.fair

    if m.get("marketType") == "moneyline":
        if has_draw or not long_side or not short_side:
            return []
        a, b = side_team(long_side, line), side_team(short_side, line)
        if not a or not b or a == b:
            return []
        return [
            Leg(token(slug, "yes"), line.fair[a], long_side.get("description") or a, **common, **q["yes"]),
            Leg(token(slug, "no"), line.fair[b], short_side.get("description") or b, **common, **q["no"]),
        ]

    if m.get("marketType") == "drawable_outcome":
        if not has_draw or not long_side:
            return []
        title = (m.get("titleShort") or "").lower()
        if (long_side.get("team") or {}).get("name") is None and ("tie" in title or "draw" in title or slug.endswith("-draw")):
            p, what = line.fair["Draw"], "Draw"
        else:
            team = side_team(long_side, line)
            if not team:
                return []
            p, what = line.fair[team], team
        label = m.get("titleShort") or what
        return [
            Leg(token(slug, "yes"), p, f"Yes: {label}", **common, **q["yes"]),
            Leg(token(slug, "no"), 1 - p, f"No: {label}", **common, **q["no"]),
        ]
    return []


def match(line: Line, events: list[dict]) -> list[Leg]:
    legs = []
    for event in events:
        if event.get("closed"):
            continue
        # Cheap pre-filter: the event must mention both teams.
        teams = [n for t in event.get("teams") or [] for n in (t.get("name"), t.get("alias"), t.get("safeName")) if n]
        text = " ".join([event.get("title") or "", *teams])
        if not all(any(same_team(n, t) for n in teams) or mentions(text, t) for t in (line.home, line.away)):
            continue
        for m in event.get("markets") or []:
            legs += legs_for_market(m, event, line)
    markets = {leg.market_id for leg in legs}
    if len(markets) > 3:  # one moneyline, or home/draw/away: anything more is ambiguous
        log.warning("%s vs %s matched %d markets; skipping as ambiguous", line.home, line.away, len(markets))
        return []
    return legs


def side_book(book: dict, side: str) -> dict:
    """Order book for buying one side, in the bot's {"asks": [{"price", "size"}], "bids": [...]} shape.
    Buying No means hitting Yes bids, at 1 - bid."""
    data = book.get("marketData") or book
    bids = [(float(l["px"]["value"]), float(l["qty"])) for l in data.get("bids") or []]
    offers = [(float(l["px"]["value"]), float(l["qty"])) for l in data.get("offers") or []]
    if side == "yes":
        asks, own_bids = offers, bids
    else:
        asks = [(round(1 - p, 4), q) for p, q in bids]
        own_bids = [(round(1 - p, 4), q) for p, q in offers]
    as_dicts = lambda levels: [{"price": p, "size": q} for p, q in levels]  # noqa: E731
    return {"asks": as_dicts(asks), "bids": as_dicts(own_bids), "min_order_size": 0.01}


class PolymarketUS:
    name = "polymarket-us"
    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, client: httpx.Client | None = None, base_url: str = GATEWAY, min_interval: float = 0.1,
                 book_interval: float = 3.0, retries: int = 4, book_ttl: float = 30.0, sleep=time.sleep,
                 clock=time.monotonic):
        self.http = client or httpx.Client(timeout=15)
        self.base = base_url.rstrip("/")
        # Measured November 2026: listings and event/market lookups take several requests a second,
        # but order books (and BBO) allow about 5 per 15 s per IP before a 10 s Retry-After. So
        # books are spaced 3 s apart, and only fetched when the listing quote shows a possible edge.
        self.min_interval = min_interval
        self.book_interval = book_interval
        self.retries = retries
        self.book_ttl = book_ttl
        self.sleep, self.clock = sleep, clock
        self._last: dict[bool, float] = {}
        self._books: dict[str, tuple[float, dict]] = {}

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        is_book = path.endswith(("/book", "/bbo"))
        gap = self.book_interval if is_book else self.min_interval
        for attempt in range(self.retries + 1):
            last = self._last.get(is_book)
            if last is not None:
                wait = last + gap - self.clock()
                if wait > 0:
                    self.sleep(wait)
            self._last[is_book] = self.clock()
            try:
                r = self.http.get(f"{self.base}{path}", params=params)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                self.sleep(2**attempt)
                continue
            if r.status_code in self.RETRY_STATUS and attempt < self.retries:
                after = r.headers.get("Retry-After", "")
                # Docs: wait at least 1 second, then back off exponentially.
                self.sleep(float(after) if after.isdigit() else max(1.0, 2**attempt) * (1 + random.random() * 0.25))
                continue
            r.raise_for_status()
            return r
        raise RuntimeError("unreachable")

    def events(self, sport: str, page: int = 100, max_pages: int = 20) -> list[dict]:
        league = LEAGUES.get(sport)
        if not league:
            raise ValueError(f"no Polymarket US league known for {sport}; add it to LEAGUES in pmus.py")
        events: list[dict] = []
        for _ in range(max_pages):
            r = self._get(f"/v2/leagues/{league}/events", params={"limit": page, "offset": len(events)})
            batch = r.json().get("events") or []
            events += batch
            if len(batch) < page:
                break
        return events

    def match(self, line: Line, events: list[dict]) -> list[Leg]:
        return match(line, events)

    def book(self, token_id: str) -> dict:
        """One request per market however many outcomes share it, reused for `book_ttl` seconds."""
        slug, side = split_token(token_id)
        cached = self._books.get(slug)
        if cached is None or self.clock() - cached[0] > self.book_ttl:
            cached = self._books[slug] = (self.clock(), self._get(f"/v1/markets/{slug}/book").json())
        return side_book(cached[1], side)

    def fee_rate(self, leg: Leg) -> float:
        return leg.fee_rate if leg.fee_rate is not None else 0.0695  # standard taker theta (docs: Fee Schedule)

    def payout_per_share(self, token_id: str, market_id: str | None = None) -> float | None:
        """1 or 0 once the market has resolved, else None."""
        slug, side = split_token(token_id)
        m = self._get(f"/v1/market/slug/{slug}").json().get("market") or {}
        if m.get("status") != "MARKET_STATUS_RESOLVED":
            return None
        prices = [float(p) for p in _json_list(m.get("outcomePrices"))]
        if len(prices) != 2 or max(prices) < 0.99:
            return None
        return prices[0] if side == "yes" else prices[1]

