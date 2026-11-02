"""The +EV bot: each cycle, price Polymarket sports moneylines against Pinnacle and buy what is cheap."""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import GAMMA_HOST, Settings, StrategySettings
from ..markets import _json_list
from ..trader import Trader
from .ev import edge, fee_per_share, kelly_fraction, plan_buy
from .ledger import Ledger
from .matching import Leg, SportsMarkets, match
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
        sports: SportsMarkets,
        trader: Trader,
        ledger: Ledger,
        live: bool = False,
        book: Callable[[str], dict] | None = None,
        fee_rate_bps: Callable[[str], float] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if live and settings.dry_run:
            raise RuntimeError("live trading needs DRY_RUN=false in .env as well as --live")
        if live and not settings.can_trade:
            raise RuntimeError("live trading needs POLY_PRIVATE_KEY")
        self.settings, self.strat = settings, strat
        self.odds, self.sports, self.trader, self.ledger = odds, sports, trader, ledger
        self.mode = "live" if live else "paper"
        self.book = book or trader.order_book
        self.fee_rate_bps = fee_rate_bps or trader.public.get_fee_rate_bps
        self.clock = clock

    # --- one pass over all sports -----------------------------------------------

    def cycle(self, execute: bool = True) -> list[Decision]:
        decisions = []
        for sport in self.strat.sports:
            try:
                lines = self.odds.lines(sport)
                events = self.sports.events(sport)
            except Exception as exc:  # one bad feed shouldn't stop the others
                log.warning("skipping %s: %s", sport, exc)
                continue
            for line in lines:
                for leg in match(line, events):
                    try:
                        decisions.append(self.evaluate(sport, leg, execute))
                    except Exception as exc:  # e.g. a book request or order failing; keep going
                        log.warning("error on %s: %s", leg.label, exc)
                        decisions.append(Decision(leg, None, None, None, "skip", f"error: {exc}"))
        if execute:
            self.settle()
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

        book = self.book(leg.token_id)
        asks, bids = levels(book.get("asks")), levels(book.get("bids"))
        if not asks:
            return skip("no asks")
        best_ask = min(p for p, _ in asks)
        best_bid = max((p for p, _ in bids), default=None)
        mid = (best_ask + best_bid) / 2 if best_bid is not None else None
        fee = float(self.fee_rate_bps(leg.token_id) or 0)
        e = edge(leg.fair, best_ask, fee)
        info = dict(ask=best_ask, mid=mid, edge=e)

        if mid is not None and abs(mid - leg.fair) > self.strat.max_divergence:
            return skip("Polymarket and Pinnacle disagree too much; likely a bad match", **info)
        if e < self.strat.min_edge:
            return skip("edge below threshold", **info)

        s = self.strat
        kelly_usd = kelly_fraction(leg.fair, best_ask + fee_per_share(best_ask, fee)) * s.kelly_multiplier * s.bankroll_usd
        budget = min(
            kelly_usd,
            self.settings.max_order_usd,
            s.max_game_exposure_usd - self.ledger.game_exposure(self.mode, line.event_id),
            s.max_daily_spend_usd - self.ledger.spent_today(self.mode, now),
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
        """Record payouts for resolved markets. In live mode this mirrors, not replaces, Polymarket's own redemption."""
        settled = 0
        markets = {}
        for f in self.ledger.unsettled(self.clock()):
            try:
                m = markets.get(f["market_id"]) or self.sports.http.get(f"{GAMMA_HOST}/markets/{f['market_id']}").json()
            except Exception as exc:
                log.warning("could not check market %s: %s", f["market_id"], exc)
                continue
            markets[f["market_id"]] = m
            if not m.get("closed"):
                continue
            prices = [float(p) for p in _json_list(m.get("outcomePrices"))]
            tokens = _json_list(m.get("clobTokenIds"))
            if f["token_id"] not in tokens or not prices or max(prices) < 0.99:
                continue  # not resolved cleanly yet
            self.ledger.settle(f["id"], f["size"] * prices[tokens.index(f["token_id"])])
            settled += 1
        return settled

    # --- loop --------------------------------------------------------------------

    def run(self, interval: float, max_cycles: int | None = None) -> None:
        n = 0
        while max_cycles is None or n < max_cycles:
            if STOP_FILE.exists():
                log.warning("%s exists; stopping", STOP_FILE)
                return
            started = self.clock()
            decisions = self.cycle()
            buys = [d for d in decisions if d.action == "buy"]
            log.info(
                "%s cycle: %d outcomes checked, %d buys, odds credits left: %s",
                datetime.now(timezone.utc).strftime("%H:%M:%S"),
                len(decisions),
                len(buys),
                self.odds.credits_remaining,
            )
            n += 1
            if max_cycles is not None and n >= max_cycles:
                return
            time.sleep(max(0.0, interval - (self.clock() - started)))
