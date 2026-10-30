"""Market discovery via Polymarket's public Gamma and Data APIs."""

import json
from typing import Any

import httpx

from .config import DATA_HOST, GAMMA_HOST


def _json_list(value: Any) -> list:
    # Gamma returns several list fields as JSON-encoded strings.
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value or []


def normalize_market(m: dict) -> dict:
    outcomes = _json_list(m.get("outcomes"))
    prices = _json_list(m.get("outcomePrices"))
    tokens = _json_list(m.get("clobTokenIds"))
    return {
        "id": m.get("id"),
        "question": m.get("question"),
        "slug": m.get("slug"),
        "condition_id": m.get("conditionId"),
        "end_date": m.get("endDate"),
        "volume_24h": float(m.get("volume24hr") or 0),
        "liquidity": float(m.get("liquidity") or 0),
        "neg_risk": bool(m.get("negRisk")),
        "tick_size": m.get("orderPriceMinTickSize"),
        "min_size": m.get("orderMinSize"),
        "accepting_orders": bool(m.get("acceptingOrders", m.get("enableOrderBook"))),
        "outcomes": [
            {
                "name": name,
                "price": float(prices[i]) if i < len(prices) else None,
                "token_id": tokens[i] if i < len(tokens) else None,
            }
            for i, name in enumerate(outcomes)
        ],
    }


class MarketData:
    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=15)

    def top_markets(self, limit: int = 25, offset: int = 0) -> list[dict]:
        r = self.http.get(
            f"{GAMMA_HOST}/markets",
            params={
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": limit,
                "offset": offset,
            },
        )
        r.raise_for_status()
        return [normalize_market(m) for m in r.json()]

    def search(self, query: str, limit: int = 25) -> list[dict]:
        r = self.http.get(
            f"{GAMMA_HOST}/public-search",
            params={"q": query, "limit_per_type": limit, "events_status": "active"},
        )
        r.raise_for_status()
        markets = []
        for event in r.json().get("events") or []:
            for m in event.get("markets") or []:
                if not m.get("closed"):
                    markets.append(normalize_market(m))
        return markets[:limit]

    def market(self, market_id: str) -> dict:
        r = self.http.get(f"{GAMMA_HOST}/markets/{market_id}")
        r.raise_for_status()
        return normalize_market(r.json())

    def positions(self, user: str) -> list[dict]:
        r = self.http.get(f"{DATA_HOST}/positions", params={"user": user, "sizeThreshold": 0.01})
        r.raise_for_status()
        return r.json()
