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

## Sports bot: trading against Pinnacle

`python -m tradeorama.strategy` runs an automated strategy. It treats Pinnacle's odds, with the bookmaker's margin removed, as the true probability of each sports result. When Polymarket sells an outcome for enough less than that, the bot buys it.

### How it decides

1. It fetches Pinnacle moneylines through [The Odds API](https://the-odds-api.com). Pinnacle closed its own public API in 2025.
2. It finds the matching Polymarket markets by team names and start time. This covers team-vs-team markets, and for soccer the "Will X win?" and "draw" markets. It skips spreads, totals, half/quarter markets and anything ambiguous.
3. For each outcome, it calculates **edge = fair probability − best ask − fees**.
4. It buys only when the edge is at least `MIN_EDGE`. It takes order-book levels only while each one still clears that edge, using a fill-and-kill order that never rests on the book.
5. Bet size is `KELLY_MULTIPLIER` × the Kelly stake on `BANKROLL_USD`. It is then capped by `MAX_ORDER_USD`, `MAX_GAME_EXPOSURE_USD` and `MAX_DAILY_SPEND_USD`.

The bot also skips a market when:
- Pinnacle's price is older than `MAX_ODDS_AGE_SEC`
- the game starts within `MIN_MINUTES_BEFORE_START`
- Polymarket's midpoint is more than `MAX_DIVERGENCE` from Pinnacle's, which almost always means a bad match rather than free money

### Judging whether it works: closing line value

Profit over a few weeks is mostly luck. The better early signal is **closing line value (CLV)**: how Pinnacle's last price before the game compares with what you paid. If you consistently buy below the closing price, the edges are real. If average CLV is zero or negative, the bot is trading on noise and losing to fees and spread, however its profit looks. The bot records the closing price for every outcome it trades.

### Commands

```bash
python -m tradeorama.strategy scan     # show matched markets and edges right now; trades nothing
python -m tradeorama.strategy run      # paper trade every 5 minutes (simulated against the live order book)
python -m tradeorama.strategy report   # per-sport trades, average edge, average CLV, settled P&L and ROI
python -m tradeorama.strategy settle   # record results for finished games (run also does this)
python -m tradeorama.strategy run --live   # real orders; also needs DRY_RUN=false
```

To stop a running bot, press Ctrl+C or create a file named `tradeorama.stop` in the folder it runs from. The web UI shows bot trades in the "Sports bot" panel.

### Suggested path

1. Run `scan` and read every row. Each one must be the right game and the right side. A wrong match is the most expensive bug this bot can have.
2. Paper trade for 2–4 weeks, until there are at least 100 trades.
3. Go live only if average CLV is clearly positive (a cent or more), with small limits at first.
4. Check CLV for each sport separately, since `report` and the web panel split results by sport. Pinnacle is sharper in some sports than others. Drop any sport whose CLV stays at zero or below once it has about 100 trades with a closing line (the `(n)` column).

### Costs and limits

- Each check uses one Odds API request per sport. To save credits, `run` checks each sport on its own schedule, based on when that sport's next game starts:

  | Next game starts in | Checks |
  |---|---|
  | under 2 hours | every `--interval` (default 5 minutes) |
  | 2–12 hours | every 15 minutes |
  | more than 12 hours | hourly |
  | no games listed | every 6 hours |

  A typical NBA day comes to about 115 requests, roughly 3,400 a month per sport, against 8,640 for checking every 5 minutes around the clock. Add `--fixed` to check every `--interval` regardless. The tiers are set by the `POLL_*` settings in `.env`. The free tier (500 a month) is enough for `scan` and occasional runs.
- Fees: the bot reads each market's fee rate from Polymarket and subtracts `rate × min(p, 1−p)` per share.
- The bot only buys and holds to the end of the game. It doesn't sell early or hedge.
- Market matching relies on how Polymarket names and times its sports markets. If Polymarket changes that, matches will drop to zero, which is the safe failure.
- In live mode, the bot's ledger records what each order reported. Your Polymarket positions are the final record.

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
| GET | `/api/strategy` | Sports bot trades and summary |

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
  strategy/    Pinnacle +EV sports bot: odds feed, market matching, EV/Kelly math, SQLite ledger, CLI
  static/      single-page web UI
tests/
```
