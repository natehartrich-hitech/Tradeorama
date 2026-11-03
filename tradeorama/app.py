"""FastAPI server exposing market data and trading, plus a small web UI."""

from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, load_settings, load_strategy_settings, setup_problems
from .markets import MarketData
from .trader import OrderRejected, SetupProblem, Trader, TradingDisabled

STATIC_DIR = Path(__file__).parent / "static"


class OrderRequest(BaseModel):
    token_id: str
    side: Literal["BUY", "SELL"]
    kind: Literal["limit", "market"] = "limit"
    price: float | None = Field(None, description="Limit price, 0-1 (limit orders)")
    size: float | None = Field(None, description="Shares (limit orders)")
    amount: float | None = Field(None, description="USDC for BUY, shares for SELL (market orders)")
    order_type: Literal["GTC", "FOK", "FAK"] = "GTC"


def create_app(settings: Settings | None = None, trader: Trader | None = None, markets: MarketData | None = None) -> FastAPI:
    settings = settings or load_settings()
    trader = trader or Trader(settings)
    markets = markets or MarketData()

    app = FastAPI(title="Tradeorama", version="0.1.0")

    @app.exception_handler(OrderRejected)
    async def _rejected(_: Request, exc: OrderRejected):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(TradingDisabled)
    async def _disabled(_: Request, exc: TradingDisabled):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(SetupProblem)
    async def _setup(_: Request, exc: SetupProblem):
        return JSONResponse(status_code=400, content={"detail": f"Setup problem in .env: {exc}"})

    @app.exception_handler(httpx.HTTPError)
    async def _upstream(_: Request, exc: httpx.HTTPError):
        return JSONResponse(status_code=502, content={"detail": f"Polymarket API error: {exc}"})

    @app.get("/api/status")
    def status():
        problems = setup_problems(settings)
        return {
            "trading_enabled": settings.can_trade,
            "dry_run": settings.dry_run,
            "max_order_usd": settings.max_order_usd,
            "address": None if problems else trader.address(),
            "problems": problems,
        }

    @app.get("/api/markets")
    def list_markets(q: str | None = None, limit: int = 25, offset: int = 0):
        if q:
            return markets.search(q, limit=limit)
        return markets.top_markets(limit=limit, offset=offset)

    @app.get("/api/markets/{market_id}")
    def get_market(market_id: str):
        return markets.market(market_id)

    @app.get("/api/book/{token_id}")
    def order_book(token_id: str):
        return trader.order_book(token_id)

    @app.get("/api/positions")
    def positions():
        address = trader.address()
        if not address:
            raise TradingDisabled("POLY_PRIVATE_KEY is not set; no account to show positions for")
        return markets.positions(address)

    @app.get("/api/orders")
    def open_orders(token_id: str | None = None):
        return trader.open_orders(token_id)

    @app.post("/api/orders")
    def place_order(req: OrderRequest):
        if req.kind == "market":
            if req.amount is None:
                raise HTTPException(400, "market orders require 'amount'")
            return trader.place_market_order(req.token_id, req.side, req.amount)
        if req.price is None or req.size is None:
            raise HTTPException(400, "limit orders require 'price' and 'size'")
        return trader.place_limit_order(req.token_id, req.side, req.price, req.size, req.order_type)

    @app.delete("/api/orders/{order_id}")
    def cancel_order(order_id: str):
        return trader.cancel(order_id)

    @app.delete("/api/orders")
    def cancel_all():
        return trader.cancel_all()

    @app.get("/api/strategy")
    def strategy_report(mode: Literal["paper", "live"] | None = None, limit: int = 50):
        strat = load_strategy_settings()
        path = Path(strat.db_path)
        if not path.exists():
            return {"summary": None, "by_sport": [], "trades": []}
        from .strategy.ledger import Ledger

        ledger = Ledger(str(path))
        by_sport = [
            {"mode": m, "sport": sport, **row}
            for m in ([mode] if mode else ["paper", "live"])
            for sport, row in ledger.summary_by_sport(m, strat.venue).items()
        ]
        return {
            "venue": strat.venue,
            "summary": ledger.summary(mode, strat.venue),
            "by_sport": by_sport,
            "trades": ledger.trades(mode, limit, venue=strat.venue),
        }

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


def main():
    import logging

    import uvicorn

    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can contain API keys

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
