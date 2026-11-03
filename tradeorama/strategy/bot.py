"""The +EV bot: each cycle, price Polymarket sports moneylines against Pinnacle and buy what is cheap."""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import Settings, StrategySettings
from ..trader import Trader
from .ev import edge, fee_per_share, kelly_fraction, plan_buy
from .ledger import Ledger
from .matching import Leg, PolymarketIntl
from .odds import PinnacleOdds

log = logging.getLogger("tradeorama.strategy")
STOP_FILE = Path("tradeorama.stop")


@dataclass
class Decision:
    leg: Leg
    best_ask: float | None
    mid: float | None
    edge: float | None
    action: str  # 'buy', 'skip'
    reason: str = ""
    size: float = 0.0
    cost: float = 0.0


def levels(side: list[dict]) -> list[tuple[float, float]]:
    return [(float(level["price"]), float(level["size"])) for level in side or []]


class Bot:
    def __init__(
        self,
        settings: Settings,
        strat: StrategySettings,
        odds: PinnacleOdds,
        venue,
        trader: Trader,
        ledger: Ledger,
        live: bool = False,
        book: Callable[[str], dict] | None = None,
        fee_rate: Callable[[str], float] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if not hasattr(venue, "match"):  # a bare SportsMarkets: the international site
            venue = PolymarketIntl(venue, book or trader.order_book, fee_rate or (lambda token_id: strat.taker_fee_rate))
        if live and venue.name != "polymarket":
            raise RuntimeError(f"live trading isn't supported on {venue.name} yet; this bot only paper trades there")
        if live and settings.dry_run:
            raise RuntimeError("live trading needs DRY_RUN=false in .env as well as --live")
        if live and not settings.can_trade:
            raise RuntimeError("live trading needs POLY_PRIVATE_KEY")
        self.settings, self.strat = settings, strat
        self.odds, self.venue, self.trader, self.ledger = odds, venue, trader, ledger
        self.mode = "live" if live else "paper"
        self.clock = clock
        self.next_start: dict[str, float | None] = {}  # per sport: start time of the next game not yet started
        self.fetch_failed: dict[str, bool] = {}

    # --- one pass over all sports -----------------------------------------------

    def cycle(self, execute: bool = True, sports: list[str] | None = None) -> list[Decision]:
        decisions = []
        for sport in sports or self.strat.sports:
            decisions += self.cycle_sport(sport, execute)
        if execute:
            self.settle()
        return decisions

    def cycle_sport(self, sport: str, execute: bool = True) -> list[Decision]:
        decisions = []
        try:
            lines = self.odds.lines(sport)
            events = self.venue.events(sport)
        except Exception as exc:  # one bad feed shouldn't stop the others
            log.warning("skipping %s: %s", sport, exc)
            self.next_start[sport] = None
            self.fetch_failed[sport] = True
            return decisions
        self.fetch_failed[sport] = False
        cutoff = self.clock() + self.strat.min_minutes_before_start * 60
        upcoming = [line.commence.timestamp() for line in lines if line.commence.timestamp() > cutoff]
        self.next_start[sport] = min(upcoming, default=None)
        for line in lines:
            for leg in self.venue.match(line, events):
                try:
                    decisions.append(self.evaluate(sport, leg, execute))
                except Exception as exc:  # e.g. a book request or order failing; keep going
                    log.warning("error on %s: %s", leg.label, exc)
                    decisions.append(Decision(leg, None, None, None, "skip", f"error: {exc}"))
        return decisions

    def evaluate(self, sport: str, leg: Leg, execute: bool) -> Decision:
        now = self.clock()
        line = leg.line
        commence = line.commence.timestamp()
        self.ledger.record_closing(leg.token_id, leg.fair, commence, now)

        def skip(reason, **kw):
            return Decision(leg, kw.get("ask"), kw.get("mid"), kw.get("edge"), "skip", reason)

        if now - line.updated.timestamp() > self.strat.max_odds_age_sec:
            return skip("Pinnacle price is stale")
        if commence - now < self.strat.min_minutes_before_start * 60:
            return skip("game starting or started")

        fee = self.venue.fee_rate(leg)
        if leg.quote_ask is not None:
            # The listing's best price already rules out a trade: don't fetch the order book.
            e0 = edge(leg.fair, leg.quote_ask, fee)
            mid0 = (leg.quote_ask + leg.quote_bid) / 2 if leg.quote_bid is not None else None
            info0 = dict(ask=leg.quote_ask, mid=mid0, edge=e0)
            if mid0 is not None and abs(mid0 - leg.fair) > self.strat.max_divergence:
                return skip("market and Pinnacle disagree too much; likely a bad match", **info0)
            if e0 < self.strat.min_edge:
                return skip("edge below threshold", **info0)
        elif leg.quote_bid is not None and leg.quote_ask is None:
            return skip("no asks", ask=None, mid=None, edge=None)

        book = self.venue.book(leg.token_id)
        asks, bids = levels(book.get("asks")), levels(book.get("bids"))
        if not asks:
            return skip("no asks")
        best_ask = min(p for p, _ in asks)
        best_bid = max((p for p, _ in bids), default=None)
        mid = (best_ask + best_bid) / 2 if best_bid is not None else None
        e = edge(leg.fair, best_ask, fee)
        info = dict(ask=best_ask, mid=mid, edge=e)

        if mid is not None and abs(mid - leg.fair) > self.strat.max_divergence:
            return skip("market and Pinnacle disagree too much; likely a bad match", **info)
        if e < self.strat.min_edge:
            return skip("edge below threshold", **info)

        s = self.strat
        kelly_usd = kelly_fraction(leg.fair, best_ask + fee_per_share(best_ask, fee)) * s.kelly_multiplier * s.bankroll_usd
        budget = min(
            kelly_usd,
            self.settings.max_order_usd,
            s.max_game_exposure_usd - self.ledger.game_exposure(self.mode, line.event_id, self.venue.name),
            s.max_daily_spend_usd - self.ledger.spent_today(self.mode, now, self.venue.name),
        )
        if budget < 1:
            return skip("risk limit reached or bet too small", **info)
        fill = plan_buy(leg.fair, asks, budget, s.min_edge, fee)
        min_size = float(book.get("min_order_size") or 0)
        if fill is None or fill.size < min_size or fill.cost < 1:
            return skip("not enough size at a good price", **info)

        decision = Decision(leg, best_ask, mid, e, "buy", size=fill.size, cost=fill.cost)
        if not execute:
            return decision

        order_id = None
        size, cost = fill.size, fill.cost
        # The per-order cap is checked against the worst price taken, which can sit above the average.
        if fill.limit_price * size > self.settings.max_order_usd:
            size = int(self.settings.max_order_usd / fill.limit_price * 100) / 100
            cost = fill.cost * size / fill.size
            decision.size, decision.cost = size, cost
        if self.mode == "live":
            # FAK at the worst acceptable price: takes what's there now, never rests on the book.
            res = self.trader.place_limit_order(leg.token_id, "BUY", fill.limit_price, size, "FAK")
            resp = res.get("response") or {}
            order_id = resp.get("orderID")
            # For a BUY, makingAmount is USDC paid and takingAmount is shares received.
            if resp.get("takingAmount") not in (None, ""):
                size, cost = float(resp["takingAmount"]), float(resp.get("makingAmount") or cost)
            if not resp.get("success", True) or size <= 0:
                decision.action, decision.reason = "skip", f"order not filled: {resp.get('errorMsg') or resp}"
                return decision
            decision.size, decision.cost = size, cost

        self.ledger.record_fill(
            ts=now,
            venue=self.venue.name,
            mode=self.mode,
            sport=sport,
            event_id=line.event_id,
            commence=commence,
            market_id=leg.market_id,
            token_id=leg.token_id,
            label=leg.label,
            question=leg.question,
            size=size,
            cost=cost,
            fair=leg.fair,
            edge=fill.edge_per_share,
            order_id=order_id,
        )
        log.info("%s BUY %.2f %s for $%.2f (fair %.3f, edge %.1f¢)", self.mode, size, leg.label, cost, leg.fair, 100 * fill.edge_per_share)
        return decision

    # --- settlement --------------------------------------------------------------

    def settle(self) -> int:
        """Record payouts for resolved markets. In live mode this mirrors, not replaces, the exchange's own settlement."""
        settled = 0
        seen: dict[str, float | None] = {}
        for f in self.ledger.unsettled(self.clock(), self.venue.name):
            if f["token_id"] not in seen:
                try:
                    seen[f["token_id"]] = self.venue.payout_per_share(f["token_id"], f["market_id"])
                except Exception as exc:
                    log.warning("could not check market %s: %s", f["market_id"], exc)
                    continue
            per_share = seen[f["token_id"]]
            if per_share is None:
                continue  # not resolved yet
            self.ledger.settle(f["id"], f["size"] * per_share)
            settled += 1
        return settled

    # --- loop --------------------------------------------------------------------

    def run(self, interval: float, max_cycles: int | None = None, adaptive: bool = True) -> None:
        """Poll each sport on its own schedule: every `interval` seconds close to game time, less often otherwise."""
        due = {sport: self.clock() for sport in self.strat.sports}
        n = 0
        while max_cycles is None or n < max_cycles:
            if STOP_FILE.exists():
                log.warning("%s exists; stopping", STOP_FILE)
                return
            now = self.clock()
            ready = [sport for sport, t in due.items() if t <= now]
            if not ready:
                # Sleep in short steps so the stop file is noticed promptly.
                time.sleep(min(30.0, min(due.values()) - now))
                continue
            decisions = self.cycle(sports=ready)
            buys = [d for d in decisions if d.action == "buy"]
            plans = []
            for sport in ready:
                if adaptive and not self.fetch_failed.get(sport):
                    delay = poll_delay(self.next_start.get(sport), self.clock(), interval, self.strat)
                else:
                    delay = interval
                due[sport] = self.clock() + delay
                plans.append(f"{sport} again in {delay / 60:.0f} min")
            log.info(
                "%s: %d outcomes checked, %d buys, odds credits left: %s; %s",
                datetime.now(timezone.utc).strftime("%H:%M:%S"),
                len(decisions),
                len(buys),
                self.odds.credits_remaining,
                ", ".join(plans),
            )
            n += 1


def poll_delay(next_start: float | None, now: float, near_interval: float, strat: StrategySettings) -> float:
    """Seconds until a sport's odds are worth fetching again, given its next game's start time.

    Prices move most in the last hours before a game, so poll fast then and slowly otherwise.
    Never sleep past the moment the next game moves into a faster tier.
    """
    if next_start is None:
        return strat.poll_idle_sec
    until = next_start - now
    near, mid = strat.poll_near_hours * 3600, strat.poll_mid_hours * 3600
    if until <= near:
        return near_interval
    if until <= mid:
        return max(near_interval, min(strat.poll_mid_sec, until - near))
    return max(near_interval, min(strat.poll_far_sec, until - mid))
