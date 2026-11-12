"""Did Kalshi trade far enough from Pinnacle's no-vig line to pay after fees, and did those prices beat the close?

Pure functions, no I/O. Prices and probabilities are dollars per $1 contract; times are Unix seconds.

How a trade is judged:
- A Kalshi trade is a real fill, so its price was really on offer, at least for its size. A trade whose taker
  ended up holding YES on the Utah market means Utah was bought at the YES price. One holding NO there means the
  other team was bought at the NO price (1 − YES).
- Each trade is compared with the latest Pinnacle snapshot taken at or before it, never a later one.
- It counts as a gap only if Pinnacle's line for that team was the same (within `tolerance`) in the next
  snapshot too. Otherwise the "gap" may only be Pinnacle moving first and Kalshi following.
- Trades at least `max_divergence` away from Pinnacle are left out: like the bot, that almost always means a
  bad match or bad data rather than free money.
- Qualifying trades between two snapshots form one "window" for that team: one chance to buy, sized by what
  traded at prices that cleared the edge.
- CLV is defined as in the bot's ledger: Pinnacle's closing no-vig probability minus the cost per contract,
  fees included.
"""

from __future__ import annotations

import bisect
import random
import statistics
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Snapshot:
    ts: float
    fair: dict[str, float]  # team name -> Pinnacle no-vig probability


@dataclass(frozen=True)
class Game:
    key: str  # Kalshi event ticker
    day: str  # game date, YYYY-MM-DD (US Eastern)
    commence: float
    home: str
    away: str
    snapshots: tuple[Snapshot, ...]  # taken before the start, oldest first; the last is the closing line
    winner: str | None = None


@dataclass(frozen=True, slots=True)  # slots: a month of NBA trades is ~640,000 of these
class Trade:
    ts: float
    team: str  # the team the taker bought
    price: float  # paid per contract, before fees
    contracts: float
    fee_waived: bool = False


@dataclass(frozen=True)
class Params:
    min_edge: float = 0.03  # same default as the sports bot
    hours: float = 6.0  # how long before the start to look
    fee_coef: float = 0.07  # Kalshi taker fee: coef × p × (1 − p) per contract
    il_tax: float = 0.0  # what-if: Illinois' 1.75% tax on the amount wagered
    stake: float = 100.0  # paper stake per window, capped by what traded at qualifying prices
    tolerance: float = 0.005  # Pinnacle must not move more than this between the snapshots around a trade
    max_divergence: float = 0.15
    max_age_sec: float = 35 * 60  # snapshots are 30 minutes apart; allow for the API's 5-minute grid


def fee(price: float, coef: float) -> float:
    # Kalshi rounds each order's total fee up to the next cent; on a $100 order that's under 1¢ in all.
    return coef * price * (1 - price)


def cost(price: float, prm: Params, fee_waived: bool = False) -> float:
    return price + (0.0 if fee_waived else fee(price, prm.fee_coef)) + prm.il_tax * price


@dataclass
class Window:
    game: Game
    team: str
    snapshot_ts: float
    fair: float
    close: float
    first_ts: float
    trades: int = 0
    contracts: float = 0.0
    dollars: float = 0.0  # traded at prices that cleared the edge, before fees
    cost_total: float = 0.0  # the same contracts with fees (and tax)

    @property
    def avg_price(self) -> float:
        return self.dollars / self.contracts

    @property
    def avg_cost(self) -> float:
        return self.cost_total / self.contracts

    @property
    def edge(self) -> float:
        return self.fair - self.avg_cost

    @property
    def clv(self) -> float:
        return self.close - self.avg_cost

    @property
    def hours_to_tip(self) -> float:
        return (self.game.commence - self.first_ts) / 3600

    def spend(self, stake: float) -> float:
        return min(stake, self.cost_total)

    def bought(self, stake: float) -> float:
        return self.spend(stake) / self.avg_cost

    def pnl(self, stake: float) -> float | None:
        if self.game.winner is None:
            return None
        won = self.bought(stake) if self.game.winner == self.team else 0.0
        return won - self.spend(stake)


SKIP_REASONS = {
    "before_snapshots": "before the first snapshot",
    "after_snapshots": "after the last snapshot (no later line to check against)",
    "stale": "snapshot too old (a gap in the snapshots)",
    "moved": "Pinnacle moved around the trade",
    "divergent": "too far from Pinnacle (likely a bad match or bad data)",
    "small": "edge below the minimum",
    "block": "block trade (off the order book)",
}


@dataclass
class Replay:
    prm: Params
    windows: list[Window]
    games: int = 0  # games with Pinnacle snapshots and pregame trades
    days: int = 0
    trades: int = 0  # pregame trades considered
    dollars: float = 0.0
    skipped: dict[str, int] = field(default_factory=dict)
    gaps: dict[float, float] = field(default_factory=dict)  # price − fair (to 0.1¢) -> dollars traded, fresh snapshot only


def replay(games: list[Game], trades: dict[str, list[Trade]], prm: Params, blocks: dict[str, int] | None = None) -> Replay:
    """games and trades keyed by Game.key. blocks: block trades left out per game, only for the count."""
    out = Replay(prm, [])
    days = set()
    skipped = dict.fromkeys(SKIP_REASONS, 0)
    for g in games:
        snaps = g.snapshots
        pregame = [t for t in trades.get(g.key, []) if g.commence - prm.hours * 3600 <= t.ts < g.commence]
        skipped["block"] += (blocks or {}).get(g.key, 0)
        if len(snaps) < 2 or not pregame:
            continue
        out.games += 1
        days.add(g.day)
        times = [s.ts for s in snaps]
        windows: dict[tuple[str, int], Window] = {}
        for t in pregame:
            out.trades += 1
            out.dollars += t.price * t.contracts
            k = bisect.bisect_right(times, t.ts) - 1
            if k < 0:
                skipped["before_snapshots"] += 1
                continue
            before = snaps[k]
            fair = before.fair.get(t.team)
            if fair is None:
                continue
            if t.ts - before.ts > prm.max_age_sec:
                skipped["stale"] += 1
                continue
            gap = round(t.price - fair, 3)
            out.gaps[gap] = out.gaps.get(gap, 0.0) + t.price * t.contracts
            if k + 1 >= len(snaps):
                skipped["after_snapshots"] += 1
                continue
            after = snaps[k + 1].fair.get(t.team)
            if after is None or abs(after - fair) > prm.tolerance:
                skipped["moved"] += 1
                continue
            if abs(t.price - fair) > prm.max_divergence:
                skipped["divergent"] += 1
                continue
            unit = cost(t.price, prm, t.fee_waived)
            if fair - unit < prm.min_edge:
                skipped["small"] += 1
                continue
            w = windows.get((t.team, k))
            if w is None:
                w = windows[(t.team, k)] = Window(g, t.team, before.ts, fair, snaps[-1].fair[t.team], t.ts)
            w.trades += 1
            w.contracts += t.contracts
            w.dollars += t.price * t.contracts
            w.cost_total += unit * t.contracts
        out.windows += sorted(windows.values(), key=lambda w: w.first_ts)
    out.days = len(days)
    out.skipped = skipped
    return out


def weighted_clv(windows: list[Window], stake: float) -> float | None:
    """Average CLV per contract bought, as the bot's report weights it."""
    n = sum(w.bought(stake) for w in windows)
    return sum(w.clv * w.bought(stake) for w in windows) / n if n else None


def clv_range(windows: list[Window], stake: float, sims: int = 4000, seed: int = 1) -> tuple[float, float] | None:
    """About-95% range for average CLV, resampling game days (trades on one day share news and line moves)."""
    by_day: dict[str, list[Window]] = {}
    for w in windows:
        by_day.setdefault(w.game.day, []).append(w)
    days = list(by_day.values())
    if len(days) < 2:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(sims):
        sample = [w for _ in days for w in rng.choice(days)]
        v = weighted_clv(sample, stake)
        if v is not None:
            values.append(v)
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


@dataclass(frozen=True)
class Luck:
    games: int
    staked: float
    pnl: float
    close_expected: float  # if Pinnacle's closing line was the true probability
    chance_pct: float  # how often a no-skill trader (prices paid = true odds) does at least as well


def luck_check(windows: list[Window], stake: float, sims: int = 20000, seed: int = 1) -> Luck | None:
    """Like the weather paper report's "Is it luck?", simulated per game, since all bets on one game share a result."""
    by_game: dict[str, list[Window]] = {}
    for w in windows:
        if w.game.winner is not None:
            by_game.setdefault(w.game.key, []).append(w)
    if not by_game:
        return None
    staked = pnl = close_exp = 0.0
    sim_games = []
    for ws in by_game.values():
        g = ws[0].game
        home_bets = [(w.bought(stake), w.spend(stake)) for w in ws if w.team == g.home]
        away_bets = [(w.bought(stake), w.spend(stake)) for w in ws if w.team != g.home]
        spent = sum(w.spend(stake) for w in ws)
        staked += spent
        pnl += sum(w.pnl(stake) for w in ws)
        close_exp += sum(w.bought(stake) * w.close for w in ws) - spent
        # No skill: each price paid (fees included) is the true probability; average them per game.
        n = sum(c for c, _ in home_bets) + sum(c for c, _ in away_bets)
        p_home = (sum(s for _, s in home_bets) + sum(c - s for c, s in away_bets)) / n
        sim_games.append((min(max(p_home, 0.0), 1.0), sum(c for c, _ in home_bets), sum(c for c, _ in away_bets), spent))
    rng = random.Random(seed)
    at_least = 0
    for _ in range(sims):
        total = 0.0
        for p_home, home_c, away_c, spent in sim_games:
            total += (home_c if rng.random() < p_home else away_c) - spent
        at_least += total >= pnl - 1e-9
    return Luck(len(by_game), staked, pnl, close_exp, 100 * at_least / sims)


@dataclass(frozen=True)
class Group:
    label: str
    windows: int
    clv: float | None
    staked: float
    pnl: float


def split(windows: list[Window], stake: float, key, order: list[str]) -> list[Group]:
    groups: dict[str, list[Window]] = {}
    for w in windows:
        groups.setdefault(key(w), []).append(w)
    out = []
    for label in order:
        ws = groups.get(label, [])
        settled = [w for w in ws if w.pnl(stake) is not None]
        out.append(Group(label, len(ws), weighted_clv(ws, stake), sum(w.spend(stake) for w in settled),
                         sum(w.pnl(stake) for w in settled)))
    return out


def hours_bucket(w: Window) -> str:
    h = w.hours_to_tip
    return "under 1 h" if h < 1 else "1–3 h" if h < 3 else "3 h or more"


def side_bucket(w: Window) -> str:
    return "favorite" if w.fair >= 0.5 else "underdog"


def edge_bucket(w: Window) -> str:
    e = w.edge
    return "3–5¢" if e < 0.05 else "5–10¢" if e < 0.10 else "10¢ or more"


@dataclass(frozen=True)
class Rule:
    text: str
    value: str
    passed: bool


def kill_rules(rep: Replay, min_per_day: float = 1.0, min_dollars: float = 100, min_clv: float = 0.01) -> list[Rule]:
    """The pre-set kill rules: too rare, too thin, or no closing line value."""
    sides = {(w.game.key, w.team) for w in rep.windows}
    per_day = len(sides) / rep.days if rep.days else 0.0
    median_dollars = statistics.median(w.dollars for w in rep.windows) if rep.windows else 0.0
    clv = weighted_clv(rep.windows, rep.prm.stake)
    return [
        Rule(f"At least {min_per_day:g} game-side with a gap per game day", f"{per_day:.2f}", per_day >= min_per_day),
        Rule(f"Typical gap has at least ${min_dollars:.0f} traded at qualifying prices",
             f"${median_dollars:,.0f} (median)", median_dollars >= min_dollars),
        Rule(f"Average CLV at least {100 * min_clv:+.0f}¢", "–" if clv is None else f"{100 * clv:+.2f}¢",
             clv is not None and clv >= min_clv),
    ]


def weighted_median(pairs) -> float | None:
    """pairs: (value, weight), e.g. Replay.gaps.items()."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    if not total:
        return None
    run = 0.0
    for v, w in pairs:
        run += w
        if run >= total / 2:
            return v
    return pairs[-1][0]
