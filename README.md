# Tradeorama

A small, self-hosted trading app for [Polymarket](https://polymarket.com). It has a Python (FastAPI) backend that uses Polymarket's official [`py-clob-client`](https://github.com/Polymarket/py-clob-client), and a browser UI where you can:

- browse the top markets by 24h volume, or search them
- view the live order book for each outcome (click a price to fill the ticket)
- place limit orders (GTC/FOK/FAK) or market orders (FOK)
- see and cancel open orders
- see your positions and P&L

## Safety first

- **Dry run is on by default.** With `DRY_RUN=true`, orders are validated and echoed back, but nothing is signed or sent. Set `DRY_RUN=false` only when you're ready to trade real USDC.
- **Per-order cap.** Any order whose notional is above `MAX_ORDER_USD` (default $50) is rejected before it reaches the exchange.
- **Read-only without a key.** If `POLY_PRIVATE_KEY` is empty, you can browse but not trade.
- **Confirmation for live orders.** In live mode the UI asks you to confirm every order.
- The server binds to `127.0.0.1` only. It has no login of its own, so don't expose it to a network.
- Your private key controls your funds. Keep it in `.env`, which is git-ignored, and never commit it.

Check Polymarket's terms of service for your jurisdiction before trading.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
python -m tradeorama   # open http://127.0.0.1:8000
```

### Configuring your wallet

| Your Polymarket account | `POLY_SIGNATURE_TYPE` | `POLY_PRIVATE_KEY` | `POLY_FUNDER_ADDRESS` |
|---|---|---|---|
| Email / Magic login | `1` | Export from polymarket.com → Settings | Your Polymarket profile (proxy) address |
| Browser wallet (MetaMask etc.) | `2` | That wallet's key | Your Polymarket profile (proxy) address |
| Raw EOA trading directly | `0` | The EOA key | leave empty |

API credentials (`POLY_API_KEY` etc.) are optional. If you leave them empty, they're derived from your key on first use.

EOA wallets (type 0) must also approve the Polymarket exchange contracts to spend their USDC and conditional tokens before trading. Accounts created on polymarket.com already have these approvals.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Mode (read-only / dry run / live), address, order cap |
| GET | `/api/markets?q=&limit=&offset=` | Top markets, or search results when `q` is set |
| GET | `/api/markets/{id}` | One market |
| GET | `/api/book/{token_id}` | Order book for an outcome token |
| GET | `/api/orders` | Open orders |
| POST | `/api/orders` | Place an order (see below) |
| DELETE | `/api/orders/{id}` | Cancel one order |
| DELETE | `/api/orders` | Cancel all orders |
| GET | `/api/positions` | Current positions |

```jsonc
// limit order: buy 25 shares at 40¢
{"token_id": "…", "side": "BUY", "kind": "limit", "price": 0.40, "size": 25, "order_type": "GTC"}
// market order: spend $10 (BUY amount = USDC; SELL amount = shares)
{"token_id": "…", "side": "BUY", "kind": "market", "amount": 10}
```

Interactive docs are served at `/docs`.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## Layout

```
tradeorama/
  config.py    settings loaded from env / .env
  markets.py   market discovery and positions (Gamma + Data APIs)
  trader.py    CLOB client wrapper: validation, dry run, order placement and cancels
  app.py       FastAPI routes; serves the UI
  static/      single-page web UI
tests/
```
