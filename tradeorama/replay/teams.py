"""Matching Kalshi game markets to The Odds API's games.

A Kalshi game is an event such as KXNBAGAME-26FEB28NOPUTA ("New Orleans at Utah", Feb 28 2026, US Eastern
date) with one market per team, the team's code after the last dash (…-UTA). The Odds API names teams in full
("Utah Jazz"), so codes are matched on the team nickname. Codes were checked against every archived Kalshi NBA
and NFL game market in November 2026.
"""

import re
from datetime import date, datetime, timedelta, timezone

NICKNAMES = {
    "basketball_nba": {
        "ATL": "hawks", "BKN": "nets", "BOS": "celtics", "CHA": "hornets", "CHI": "bulls", "CLE": "cavaliers",
        "DAL": "mavericks", "DEN": "nuggets", "DET": "pistons", "GSW": "warriors", "HOU": "rockets",
        "IND": "pacers", "LAC": "clippers", "LAL": "lakers", "MEM": "grizzlies", "MIA": "heat", "MIL": "bucks",
        "MIN": "timberwolves", "NOP": "pelicans", "NYK": "knicks", "OKC": "thunder", "ORL": "magic",
        "PHI": "76ers", "PHX": "suns", "POR": "blazers", "SAC": "kings", "SAS": "spurs", "TOR": "raptors",
        "UTA": "jazz", "WAS": "wizards",
    },
    "americanfootball_nfl": {
        "ARI": "cardinals", "ATL": "falcons", "BAL": "ravens", "BUF": "bills", "CAR": "panthers", "CHI": "bears",
        "CIN": "bengals", "CLE": "browns", "DAL": "cowboys", "DEN": "broncos", "DET": "lions", "GB": "packers",
        "HOU": "texans", "IND": "colts", "JAC": "jaguars", "JAX": "jaguars", "KC": "chiefs", "LA": "rams",
        "LAR": "rams", "LAC": "chargers", "LV": "raiders", "MIA": "dolphins", "MIN": "vikings", "NE": "patriots",
        "NO": "saints", "NYG": "giants", "NYJ": "jets", "PHI": "eagles", "PIT": "steelers", "SEA": "seahawks",
        "SF": "49ers", "TB": "buccaneers", "TEN": "titans", "WAS": "commanders", "WSH": "commanders",
    },
}

SERIES = {"basketball_nba": "KXNBAGAME", "americanfootball_nfl": "KXNFLGAME"}

EASTERN_STANDARD = timezone(timedelta(hours=-5))  # close enough to tell the game date: no game starts 0–1 AM ET
TICKER_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})")
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def event_date(event_ticker: str) -> date | None:
    """KXNBAGAME-26FEB28NOPUTA -> 2026-02-28."""
    parts = event_ticker.split("-")
    m = TICKER_DATE.match(parts[1]) if len(parts) > 1 else None
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def game_date(commence_ts: float) -> date:
    return datetime.fromtimestamp(commence_ts, tz=EASTERN_STANDARD).date()


def team_for_code(sport: str, code: str, names: list[str]) -> str | None:
    nick = NICKNAMES.get(sport, {}).get(code)
    if not nick:
        return None
    found = [n for n in names if re.search(rf"\b{re.escape(nick)}\b", n.lower())]
    return found[0] if len(found) == 1 else None


def match_event(sport: str, event_ticker: str, codes: list[str], games: list[dict]):
    """Find the Odds API game for a Kalshi event.

    games: Odds API events ({id, home_team, away_team, commence_ts}). Returns (game, {code: team name}) or
    (None, reason)."""
    day = event_date(event_ticker)
    if day is None:
        return None, "can't read the date in the ticker"
    if len(codes) != 2:
        return None, f"expected 2 team markets, found {len(codes)}"
    unknown = [c for c in codes if c not in NICKNAMES.get(sport, {})]
    if unknown:
        return None, f"unknown team code {', '.join(unknown)}"
    best = None
    for g in games:
        gap = abs((game_date(g["commence_ts"]) - day).days)
        if gap > 1:
            continue
        teams = [g["home_team"], g["away_team"]]
        mapping = {c: team_for_code(sport, c, teams) for c in codes}
        if None in mapping.values() or len(set(mapping.values())) != 2:
            continue
        if best is None or gap < best[0]:
            best = (gap, g, mapping)
    if best is None:
        return None, "no Odds API game with these two teams within a day"
    return best[1], best[2]
