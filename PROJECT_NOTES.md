# Project notes

Shared memory for this project: who it's for, what was decided and why, what was learned the hard way, and what's still open. The READMEs explain how to use things; this file explains the reasoning. Update it when a decision changes.

## The owner's setup

- Windows 11 PC, Chicago area, Python 3.14. Commands run in PowerShell, using `python`, not `python3`.
- **Trades on Polymarket US** (polymarket.us), the CFTC-regulated exchange for US residents. The international Polymarket site restricts US users, so it isn't the target venue.
- **Everything is paper trading.** No real orders are placed anywhere, and live trading on Polymarket US isn't built.
- The Odds API plan has 20,000 credits a month, used for Pinnacle odds.
- Progress is tracked on two checklists (artifacts in the owner's Claude account): "Tradeorama Setup Checklist" covers the app and sports bot, and "Weather Bot Checklist" covers the weather project.

## What's in the repo

| Part | Where | Status |
|---|---|---|
| Sports bot: Pinnacle +EV against Polymarket US | `tradeorama/strategy/` | Paper trading since early Nov 2026 |
| Weather model and paper trader: daily highs | `weather/` (package `wxbrackets`, own venv and `config.yaml`) | Paper trading since Nov 5, 2026 |
| Web app: browse markets, manual orders, bot panel | `tradeorama/app.py`, `tradeorama/static/` | Market browsing and orders still use the **international** site |

Both bots run at once, in separate terminal windows.

## Sports bot: key decisions

- **The source of truth is Pinnacle's no-vig moneyline**, via The Odds API with `bookmakers=pinnacle`. Pinnacle closed its own public API in 2025.
- **Sports:** `basketball_nba,icehockey_nhl,americanfootball_nfl,soccer_epl`. These are leagues where Pinnacle's line is sharpest, and together they use about 9,900 credits a month. The next sport worth adding is the Champions League.
- **Sizing and limits:** buys need a 3¢ minimum edge after fees, at quarter Kelly on a $200 bankroll, capped at $25 per game and $100 per day.
- **Odds polling slows down far from game time:** every 5 minutes within 2 hours of a game, every 15 minutes within 12 hours, hourly beyond that, and every 6 hours when no games are listed.
- **Success is judged by closing line value (CLV), per sport, not profit.** Go live only if average CLV is +1¢ or more after about 100 trades with a closing line.
- **The venue is `STRATEGY_VENUE=polymarket-us` (the default).**
  - Moneylines are one market per game. Buying the second team means paying 1 − the first team's bid.
  - Soccer has separate home, draw and away markets, settled on regulation time.
  - The fee comes from each market's `feeCoefficient` (0.0695).
  - Fills record their venue. Trades before the switch were priced from the international site and are labelled `polymarket`.

## Weather model: key decisions

- **Settlement:** Polymarket US settles on the NWS Daily Climate Report (CLI) for Chicago (**Midway**, KMDW), NYC (Central Park), Miami, LAX and SFO. A claim made in this conversation that it uses Weather Underground was wrong for Polymarket US.
- **The climate day runs midnight to midnight local standard time, all year.** During daylight saving time, that's 1 AM to 1 AM on the clock.
- **Models:**
  - ECMWF (51), GFS (31), ICON (40) and GEM (21) ensembles, from Open-Meteo, at weight 1 each.
  - HRRR (Open-Meteo) at weight 0.5.
  - RRFS at weight 0.5 and REFS (5 members) at weight 1, read directly from NOAA's AWS bucket.
  - Weights are per model, split evenly across its members.
- **Distribution:** each member's daily max is moved away from the pooled mean by `spread_inflation` 1.3 and shifted by `bias_f` (0, uncalibrated). Then a weighted Gaussian kernel is placed on each, and probabilities follow NWS whole-degree rounding.
- **Same day:**
  - The high is the larger of the observed max so far and each member's forecast for the **remaining** hours.
  - Members pinned at the observation get a kernel of `obs_tolerance_f` (0.5°F).
  - Paper trading doesn't touch today's market after noon local standard time, or without observations.
  - The earlier version kept warm afternoons alive after the peak and paper-traded stale edges. Those fills were cleared.
- **Paper trading:** 3¢ minimum edge, quarter Kelly on $200, $25 per city-day, $100 per day. It walks the real order book and settles from Polymarket US's results.
- **The deciding metric:** in `calibration`, the model's Brier score against the market's on the same runs. No real money unless the model beats the market for a city over a month or more.

## Things learned the hard way

- **Polymarket US public market data** (`gateway.polymarket.us`):
  - Order books and BBO allow about **5 requests per 15 s per IP**, then return 429 with `Retry-After: 10`. The documented 25/s is for the trading API.
  - Listings and event/market lookups aren't limited like that.
  - So both bots use listing quotes to rule out trades first, and space book requests 3 s apart.
- **International Polymarket's Gamma API** returns at most 100 events per page, whatever `limit` is asked for. Before paging was fixed, NFL games were missing.
- **Open-Meteo:** a combined multi-model ensemble request labels its keys so that none matched the model IDs (0 of 143 parsed). Each model is requested separately, where keys are `temperature_2m` / `temperature_2m_memberNN`.
- **RRFS (operational since autumn 2026):**
  - Bucket `noaa-rrfs-ops-pds`. Deterministic runs: `rrfs.YYYYMMDD/HH/rrfs.tHHz.2dfld.3km.fNNN.conus.grib2`, with `TMAX` (max over the hour). Ensemble: `rrfsens.../mMMM/...2dfldnomads...`, with `TMP` only.
  - Byte-range downloads via the `.idx` files.
  - One download is decoded for every city's grid point.
- **On Windows,** read files as UTF-8 explicitly. Otherwise cp1252 garbled "°" in `config.yaml`.
- **The Odds API puts the key in the URL.** httpx's INFO logging printed it once, so the key was rotated and httpx logging is now set to WARNING.

## Cloud-session limits (not the owner's PC)

These sessions couldn't reach Open-Meteo, api.weather.gov, forecast.weather.gov or the IEM archive, so those paths were built from docs and verified on the owner's PC. Polymarket US (`gateway.polymarket.us`, `docs.polymarket.us`), NOAA's RRFS bucket and PyPI were reachable, and were checked live.

## Known limitations of the weather model

1. Not calibrated yet. `bias_f` and `spread_inflation` are starting guesses.
2. Probably runs slightly cold: hourly data misses peaks between hours, except RRFS's `TMAX`.
3. Model grid cells (3–25 km) aren't the station's exact microclimate.
4. The bias correction is a single offset, not a statistical correction per model and situation.
5. Thin markets: wide bid/ask gaps on most brackets.

## Open items

- [ ] After about 3–4 weeks of weather data: tune `bias_f`, `spread_inflation` and model weights from `calibration`, run per city.
- [ ] Sports: judge CLV per sport at about 100 trades with a closing line; drop any sport with CLV ≤ 0.
- [ ] Not verified yet on the owner's PC: `backfill-outcomes` beyond about a week (the IEM archive path).
- [ ] Possible later: NOAA NBM probabilistic guidance (reusing the RRFS GRIB code); a time-lagged HRRR ensemble; switching the web app's browsing and orders to Polymarket US.
- [ ] Real trading on Polymarket US: not built. It would need API keys from polymarket.us/developer (Ed25519 signing, `cryptography` package) and an explicit decision by the owner.
