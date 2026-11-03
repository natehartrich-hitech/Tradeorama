"""Paper trading Polymarket US weather brackets against the model's probabilities. Places no orders."""

from dataclasses import dataclass
from datetime import datetime, timezone

from .db import DB
from .polymarket_us import BracketMarket, PolymarketUS, book_side


def fee_per_contract(price: float, fee_rate: float) -> float:
    """Polymarket US taker fee: theta * p * (1 - p) per contract (docs.polymarket.us/fees)."""
    return fee_rate * price * (1 - price)


def edge(prob: float, price: float, fee_rate: float) -> float:
    return prob - price - fee_per_contract(price, fee_rate)


def kelly_fraction(prob: float, cost: float) -> float:
    return 0.0 if cost >= 1 or prob <= cost else (prob - cost) / (1 - cost)


@dataclass
class Fill:
    contracts: float
    cost: float
    edge_per_contract: float


def plan_buy(prob: float, asks: list[tuple[float, float]], budget: float, min_edge: float, fee_rate: float) -> Fill | None:
    """Take asks cheapest first while each level still clears min_edge, up to `budget` USD."""
    contracts = cost = ev = 0.0
    for price, qty in sorted(asks):
        e = edge(prob, price, fee_rate)
        if e < min_edge or cost >= budget:
            break
        unit = price + fee_per_contract(price, fee_rate)
        take = int(min(qty, (budget - cost) / unit) * 100) / 100  # fractional contracts, 0.01 minimum
        if take <= 0:
            break
        contracts, cost, ev = contracts + take, cost + take * unit, ev + take * e
    return Fill(round(contracts, 2), round(cost, 4), ev / contracts) if contracts else None


@dataclass
class Opportunity:
    market: BracketMarket
    side: str  # "yes" / "no"
    prob: float  # model probability this side wins
    ask: float | None
    edge: float | None
    action: str = "skip"
    reason: str = ""
    fill: Fill | None = None


def opportunities(markets: list[BracketMarket], probs: list[float]) -> list[Opportunity]:
    """Both sides of every bracket, priced from the event's best quotes."""
    out = []
    for m, p in zip(markets, probs):
        for side, prob, ask in (("yes", p, m.yes_ask), ("no", 1 - p, m.no_ask)):
            out.append(Opportunity(m, side, prob, ask, None if ask is None else edge(prob, ask, m.fee_rate)))
    return out


def trade(
    db: DB,
    pmus: PolymarketUS,
    settings,
    station: str,
    target_date: str,
    run_id: int | None,
    markets: list[BracketMarket],
    probs: list[float],
    hours_left: float,
    execute: bool = True,
    now: datetime | None = None,
) -> list[Opportunity]:
    """Size and record paper buys where the model beats the market by at least min_edge after fees."""
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    opps = opportunities(markets, probs)
    for o in sorted(opps, key=lambda o: -(o.edge or -1)):
        if o.ask is None:
            o.reason = "no offer"
            continue
        if hours_left < settings.stop_hours_before_end:
            o.reason = "too close to the end of the climate day"
            continue
        if o.edge < settings.min_edge:
            o.reason = "edge below threshold"
            continue
        kelly_usd = kelly_fraction(o.prob, o.ask + fee_per_contract(o.ask, o.market.fee_rate)) * settings.kelly_multiplier * settings.bankroll_usd
        budget = min(
            kelly_usd,
            settings.max_event_usd - db.paper_spent(station=station, target_date=target_date),
            settings.max_daily_usd - db.paper_spent(since_utc=day_start),
        )
        if budget < 1:
            o.reason = "risk limit reached or bet too small"
            continue
        asks = book_side(pmus.book(o.market.slug), o.side)
        fill = plan_buy(o.prob, asks, budget, settings.min_edge, o.market.fee_rate)
        if fill is None or fill.cost < 1:
            o.reason = "not enough size at a good price"
            continue
        o.action, o.fill = "buy", fill
        if execute:
            db.record_paper_fill(
                created_utc=now.isoformat(timespec="seconds"), run_id=run_id, station=station, target_date=target_date,
                slug=o.market.slug, side=o.side, label=o.market.bracket.label, contracts=fill.contracts, cost=fill.cost,
                prob=o.prob, edge=fill.edge_per_contract,
            )
    return opps


def settle(db: DB, pmus: PolymarketUS) -> int:
    """Record payouts for paper fills whose market Polymarket US has resolved."""
    done, cache = 0, {}
    for f in db.unsettled_paper():
        if f["slug"] not in cache:
            cache[f["slug"]] = pmus.yes_payout(f["slug"])
        yes = cache[f["slug"]]
        if yes is None:
            continue
        db.settle_paper(f["id"], f["contracts"] * (yes if f["side"] == "yes" else 1 - yes))
        done += 1
    return done


def summary(db: DB, station: str | None = None) -> dict:
    fills = db.paper_fills(station)
    settled = [f for f in fills if f["payout"] is not None]
    staked = sum(f["cost"] for f in settled)
    pnl = sum(f["payout"] - f["cost"] for f in settled)
    contracts = sum(f["contracts"] for f in fills)
    return {
        "trades": len(fills),
        "staked_usd": round(sum(f["cost"] for f in fills), 2),
        "open_usd": round(sum(f["cost"] for f in fills if f["payout"] is None), 2),
        "expected_profit_usd": round(sum(f["edge"] * f["contracts"] for f in fills), 2),
        "avg_edge_cents": round(100 * sum(f["edge"] * f["contracts"] for f in fills) / contracts, 2) if contracts else None,
        "settled": len(settled),
        "settled_pnl_usd": round(pnl, 2),
        "roi_pct": round(100 * pnl / staked, 1) if staked else None,
    }
