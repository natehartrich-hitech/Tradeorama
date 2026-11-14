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

`python -m tradeorama.strategy` runs an automated strategy. It treats Pinnacle's odds, with the bookmaker's margin removed, as the true probability of each sports result. When the market sells an outcome for enough less than that, the bot buys it.

### Exchange

`STRATEGY_VENUE` picks which exchange it prices against:
- **`polymarket-us`** (default): [Polymarket US](https://polymarket.us), the CFTC-regulated exchange open to US residents. The bot reads public market data from `gateway.polymarket.us`, which needs no API key.
  - A moneyline is one market whose two sides are the two teams. Buying the second team means paying 1 − the first team's bid.
  - Soccer has separate home, draw and away markets, settled on regulation time.
  - The fee is read from each market (`feeCoefficient`, 0.0695 × p × (1 − p) as of November 2026).
  - Results come from Polymarket US's own settlement. **Paper trading only**: live orders aren't supported on Polymarket US yet.
- **`polymarket`**: the international site (gamma-api / CLOB), which is closed to US residents. Live trading through `py-clob-client` works only here.

Trades are recorded with their exchange, and `report` and the web panel show only the current one. Paper trades made before this change were priced from the international site and stay labelled that way.

### How it decides

1. It fetches Pinnacle moneylines through [The Odds API](https://the-odds-api.com). Pinnacle closed its own public API in 2025.
2. It finds the matching market for each game by team names and start time: the moneyline, or for soccer the home, draw and away markets. It skips spreads, totals, props and anything ambiguous.
3. For each outcome, it calculates **edge = fair probability − best ask − fees**.
4. It buys only when the edge is at least `MIN_EDGE`. It takes order-book levels only while each one still clears that edge, using a fill-and-kill order that never rests on the book.
5. Bet size is `KELLY_MULTIPLIER` × the Kelly stake on `BANKROLL_USD`. That's the **total** to hold in the outcome, so a later check only buys the part not already held: nothing if the edge is unchanged, a top-up if it has grown. It is also capped by `MAX_ORDER_USD`, `MAX_GAME_EXPOSURE_USD` and `MAX_DAILY_SPEND_USD`. Positions are never sold early; they're held to settlement.

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
python -m tradeorama.strategy diagnose americanfootball_nfl   # explain why a sport's games do or don't match
python -m tradeorama.strategy report --venue polymarket       # earlier trades priced from the international site
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
- Fees: the bot subtracts the taker fee, rate × p × (1 − p) per share. On Polymarket US the rate comes from each market. On the international site, it's `TAKER_FEE_RATE` (0.05 for sports, [fee table](https://docs.polymarket.com/trading/fees)).
- The bot only buys and holds to the end of the game. It doesn't sell early or hedge.
- Market matching relies on how Polymarket names and times its sports markets. If Polymarket changes that, matches will drop to zero, which is the safe failure.
- In live mode, the bot's ledger records what each order reported. Your Polymarket positions are the final record.

## Replay: Kalshi's past sports trades against Pinnacle

`python -m tradeorama.replay` tests the sports bot's idea on history before any paper trading: when Kalshi traded a game at least fees + 3¢ cheaper than Pinnacle's no-vig line, did that price beat Pinnacle's closing line? It's analysis only and places no orders. Kalshi's market data is public (no account or key). The Odds API part needs `ODDS_API_KEY` in `.env`, as for the bot, on a paid plan, since historical odds aren't on the free tier.

```powershell
python -m tradeorama.replay fetch --from 2026-02-01 --to 2026-02-28    # NBA by default; asks before spending credits
python -m tradeorama.replay report --from 2026-02-01 --to 2026-02-28 --out replay_report.txt
python -m tradeorama.replay fetch --sport americanfootball_nfl --from 2025-09-04 --to 2026-01-04
```

`fetch` caches everything in `replay.db` (`--db` to change it), so a rerun costs nothing and an interrupted one picks up where it stopped:
1. Kalshi's settled game markets for the dates (series `KXNBAGAME` or `KXNFLGAME`; `--series` for others).
2. Each day's games and start times from The Odds API (1 credit a day), matched to Kalshi's games by team and date. Unmatched games are listed with the reason.
3. Every Kalshi trade in the 6 hours before each start (`--hours`). This is free, and February 2026 took about 1½ minutes: about 640,000 trades and $73M.
4. Pinnacle snapshots every 30 minutes from 6 hours before the day's first game until its last start (`--step`). Each costs 10 credits, and one call covers every game. February 2026 is about 450 snapshots, roughly 4,500 credits. A run never spends more than `--max-credits` (default 6,000), and it stops before fewer than `--keep-credits` (default 2,500) would be left this month for the bot.

`report` replays the cache:
- **Gaps:** a trade counts only if its price clears the minimum edge (`--min-edge`, default 3¢) after Kalshi's taker fee (0.07 × p × (1 − p)) against the latest Pinnacle snapshot before it. Pinnacle's line must also be unchanged in the next snapshot. That second check stops "gaps" that were really Pinnacle moving first. Trades more than 15¢ from Pinnacle are left out as likely bad data. Qualifying trades between two snapshots form one window: one chance to buy, sized by the dollars that traded at qualifying prices.
- **CLV**, defined as in the bot: Pinnacle's closing probability minus cost per contract, fees included. The 95% range resamples game days.
- **Paper P&L** at up to `--stake` ($100) per window, with the luck check from the weather reports, and splits by hours before the start, favorite or underdog, and edge size. A what-if line adds Illinois' 1.75% tax (`--il-tax 0.0175` makes it the main result).
- **Kill rules, set before the first run.** The idea is out for that league and period if any one fails:
  - fewer than 1 game-side with a gap per game day;
  - a median under $100 traded at qualifying prices per window;
  - average CLV under +1¢.

Limits to keep in mind:
- A trade proves its price was on offer, at least for its size. Buying at it assumes nobody else took it first, so real fills would be somewhat worse.
- Trades after the last snapshot before a start (its final few minutes) can't be checked against a later line, so they're left out.
- The P&L buys again in every window while a gap lasts, unlike the bot's Kelly target and $25 game cap. CLV is the measure to trust; P&L is a rough check.
- Games whose Kalshi market closed sooner after the listed start than a whole game takes (NBA 1¾ h, NFL 2½ h) are left out and listed. Their start time is almost certainly wrong, which would make in-game trading look like pregame gaps.
- Look over the "largest gaps" list: each row must be the right game and team.

## Structural scan: Kalshi's favorite-longshot bias

`python -m tradeorama.scan` tests whether contracts priced 85–97¢ on Kalshi win more often than their price, by enough to beat fees. That's the flip side of overpriced longshots, documented by Bürgi, Deng and Whelan. It uses only Kalshi's public data (no key, no Odds API credits) and places no orders.

```powershell
python -m tradeorama.scan fetch    # samples ~10,000–60,000 settled markets and downloads hourly candles; resumable
python -m tradeorama.scan report --out scan_report.txt   # also saves a UTF-8 copy; on Windows use this, not >
python -m tradeorama.scan leads --out leads_report.txt   # the two follow-ups to the first run, same cache
```

- `fetch` picks the top series by volume in each category. It samples a few events a month from each and a few markets from each event, deterministically, by ticker hash. Then it downloads each market's hourly candles (bid, ask, trade range, volume) for the day before its scheduled time. Everything is cached in `scan.db`, and a rerun continues where it stopped.
- `report` prints three things:
  - a calibration table: how often YES won at each price, 24, 6 and 1 hour out, fit vs holdout;
  - fit-period returns by category and decision time;
  - three hypotheses chosen on Oct 2025–Apr 2026 and scored once on May–Aug 2026, against kill rules.

  The rules are fixed in `tradeorama/scan/analysis.py` and explained in PROJECT_NOTES.md ("Structural scan"). Sports get a separate verdict.
- Bets are timed from a reference fixed when each market was created: its close, when trading stops at a fixed time whatever happens, or else its scheduled expiration. Many markets close early once their result is known, so using the actual close would leak the outcome.

## Weather brackets

`weather/` is a separate project that prices daily high-temperature bracket markets from weather ensembles. See [weather/README.md](weather/README.md).

## Running the weather bot at boot

The weather paper trader can run as a Windows scheduled task. It starts 2 minutes after every boot, with nobody signed in, so a Windows Update restart in the night costs minutes, not a day. (The sports bot isn't set up this way; run it in a terminal window as before.) From an **administrator** PowerShell in this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 install    # create the task and start it now
powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 status     # running? last log lines
powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 stop       # stop it (it starts again at the next boot)
powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 start
powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 uninstall  # remove the task
```

- The task is **Weather bot** under `\Tradeorama\` in Task Scheduler. It runs `scripts\run-weather-bot.cmd` (`python -m wxbrackets paper --every 60`).
- There's no window. Output goes to `logs\weather-bot.log`, rotated when the bot starts if it's over 5 MB (one old copy is kept). To watch it live: `Get-Content .\logs\weather-bot.log -Tail 30 -Wait`.
- If the bot exits or crashes, the launcher starts it again after 2 minutes.
- `install` stops a weather bot already running in a terminal window first (it asks), since two copies would both trade.
- The task runs as you without storing your password. If `install` reports a logon error, add `-StorePassword`.

### Missed-run alerts

A PC that's off can't warn you, so each bot pings an outside "dead man's switch" while it runs, and the service alerts you when the pings stop. With [Healthchecks.io](https://healthchecks.io) (free for up to 20 checks):

1. Create one check per bot. Set **period 1 hour, grace 30 minutes**, and add an email, Telegram, ntfy or other notification.
2. Paste the sports check's ping URL into `.env` as `STRATEGY_HEARTBEAT_URL=...`, and the weather check's into `weather/config.yaml` as `heartbeat_url: "..."`.
3. Restart the bots: the weather bot with `bots.ps1 stop`, then `bots.ps1 start`; the sports bot with Ctrl+C in its window, then run it again.

Each bot pings after every pass and every 10 minutes while it waits, and reports a crash at once (`/fail`). With no URL set, nothing is sent. Stopping a bot on purpose also stops the pings, so pause its check first.

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
scripts/       bots.ps1 (run the weather bot as a scheduled task at boot) and its launcher
tradeorama/
  config.py    settings loaded from env / .env
  heartbeat.py missed-run alert pings (Healthchecks.io-style)
  markets.py   market discovery and positions (Gamma + Data APIs)
  trader.py    CLOB client wrapper: validation, dry run, order placement and cancels
  app.py       FastAPI routes; serves the UI
  strategy/    Pinnacle +EV sports bot: odds feed, market matching, EV/Kelly math, SQLite ledger, CLI
  static/      single-page web UI
tests/
```
