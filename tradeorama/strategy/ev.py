"""Edge, fee and position-size math. Pure functions, no I/O."""

from dataclasses import dataclass


def fee_per_share(price: float, fee_rate: float) -> float:
    # Polymarket taker fee per share: feeRate * p * (1 - p), largest at 50¢ (docs.polymarket.com/trading/fees).
    return fee_rate * price * (1 - price)


def edge(fair: float, price: float, fee_rate: float = 0) -> float:
    """Expected profit per share of buying at `price` when the true probability is `fair`."""
    return fair - price - fee_per_share(price, fee_rate)


def kelly_fraction(fair: float, cost: float) -> float:
    """Full-Kelly bankroll fraction for a $1-payout contract costing `cost` (fee included)."""
    if cost >= 1 or fair <= cost:
        return 0.0
    return (fair - cost) / (1 - cost)


@dataclass(frozen=True)
class Fill:
    size: float  # shares
    cost: float  # USDC, fees included
    limit_price: float  # worst price taken
    avg_cost: float  # per share, fees included
    edge_per_share: float  # average expected profit per share, after fees


def plan_buy(
    fair: float,
    asks: list[tuple[float, float]],
    budget: float,
    min_edge: float,
    fee_rate: float = 0,
) -> Fill | None:
    """Walk the asks cheapest first, taking only levels whose edge clears `min_edge`, up to `budget` USDC."""
    size = cost = ev = 0.0
    limit = 0.0
    for price, available in sorted(asks):
        e = edge(fair, price, fee_rate)
        if e < min_edge or cost >= budget:
            break
        unit = price + fee_per_share(price, fee_rate)
        take = min(available, (budget - cost) / unit)
        take = int(take * 100) / 100  # Polymarket sizes have 2 decimals
        if take <= 0:
            break
        size += take
        cost += take * unit
        ev += take * e
        limit = price
    if size <= 0:
        return None
    return Fill(
        size=round(size, 2),
        cost=round(cost, 4),
        limit_price=limit,
        avg_cost=round(cost / size, 4),
        edge_per_share=round(ev / size, 4),
    )
