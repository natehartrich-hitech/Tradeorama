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
| Weather model and paper trader: daily highs and lows | `weather/` (package `wxbrackets`, own venv and `config.yaml`) | Highs paper trading since Nov 5, 2026; lows added Nov 5 |
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
  - Paper trading doesn't trade today's high market after noon local standard time (lows: 6 AM), or without observations. After the cutoff it still **forecasts and logs prices** ("shadow mode").
  - Why the cutoff stays even though same-day forecasts are now correct: in the afternoon the market also knows the high, the NWS feed lags other traders' real-time data, and the remaining uncertainty (official versus hourly readings) is where specialists beat this model. Decision (Nov 5, 2026): **don't trade afternoons; measure them.** `calibration`'s model-vs-market table has a "today (shadow)" row. Move `same_day_last_hour` later only if the model beats the market there over weeks.
  - For the rest of today, RRFS uses the newest hourly run's 15-minute output, with the evening tail from the newest 6-hourly run. That data is about 1.5–2 hours old instead of up to a day.
  - The earlier version kept warm afternoons alive after the peak and paper-traded stale edges. Those fills were cleared.
- **Paper trading:** 3¢ minimum edge, quarter Kelly on $200, $25 per city-day, $100 per day. It walks the real order book and settles from Polymarket US's results.
- **Lead-time weighting (Nov 5, 2026):** the owner asked whether RRFS/HRRR should count more closer to the day. They probably should (3 km resolution, hourly refresh, RRFS's true hourly max), but RRFS is new and a single run is overconfident. Decision: **measure first.** `calibration` now splits each model's bias and MAE by lead (same day / next day / 2+ days). Weights stay unchanged until 3–4 weeks of data show which models earn more weight at which lead.
- **The deciding metric:** in `calibration`, the model's Brier score against the market's on the same runs. No real money unless the model beats the market for a city over a month or more.

## Daily lows (added Nov 5, 2026)

- Polymarket US has daily-low series for all five cities (`temp-{mdw,nyc,mia,lax,sfo}low-YYYY-MM-DD`). They settle on the CLI MINIMUM line for the same climate day, with the same 2°F brackets and fee. In Nov 2026, only Chicago and NYC had events listed.
- **The whole weather pipeline takes a `kind` (high/low):**
  - Model minima come from Open-Meteo, from RRFS (`TMIN` on 6-hourly runs, 15-minute `TMP` on hourly runs) and from REFS (`TMP`).
  - The observed running minimum is a ceiling. Each member's final low = min(observed, remaining-hours minimum).
  - Outcomes store `min_f`, and forecast runs and paper fills store `kind`. Old databases are migrated automatically.
  - Lows can be tuned separately via `distribution_low`.
- **Same-day cutoff for lows: 6 AM local standard time** (`same_day_last_hour_low`), since by dawn the low is mostly in. Shadow-mode scoring decides whether to move it, as for highs.
- Lows are expected to be harder (radiational cooling, Midway's urban heat, cloud and wind), so expect larger biases; calibrate them separately.

## Per-city calibration (added Nov 6, 2026)

- Measurement was already per city (`calibration --station`). Now the **settings** can be per city and per kind too: a station's own `distribution` / `distribution_low` and `weights` / `weights_low` sections layer over the shared ones. A weight of 0 drops a model for that city (and `member_weights` ignores zero-weight models).
- `calibration` prints a ready-to-paste city section with the suggested `bias_f`, and warns under **30 settled days** per city and kind, since with 2–3°F daily errors a smaller sample mostly fits noise.
- Plan: run on shared settings first; give a city its own once its bias clearly differs. Expect differences: Midway urban heat and lake breeze, Central Park trees, Miami sea breeze, LAX/SFO marine layer and fog. Biases may also shift with the seasons.

## Skill vs. luck checks (added Nov 9, 2026)

- `paper-report` "Is it luck?": settled P&L against the model's expected P&L, and against a no-skill reference (market price = true odds), with the chance that no skill does at least as well. Top-ups of one bracket are grouped, since they share one outcome. Brackets of the same event are treated as independent, which slightly overstates the variance (conservative).
- `calibration`: model-minus-market Brier with a ~95% range, using days as the independent unit.
- Why both are required before real money: Brier uses every bracket every day, so it's a fast, fill-independent test of better probabilities. Profit shows the edge survives fees, spread and trade selection. Profit alone over weeks is dominated by a few long-shot outcomes.

## Backtest (added Nov 11, 2026)

- `wxbrackets backtest`: archived runs (HRRR, RRFS, REFS, GEFS 0.5°, optional ECMWF ENS) against Polymarket US price history at a fixed decision time (00:00 UTC the evening before, runs gated by conservative publishing delays). It fits per-model bias and spread on the earlier half of each city/kind and scores the later half. Values are cached in `backtest.db` (separate from paper data, in the same folder as `database:` by default). Downloaded messages live only in memory. The first version queued every finished download for the whole run (`pool.map`), which could reach tens of GB of RAM and push Windows' page file on C; fixed Nov 11 with a bounded in-flight window and batched commits.
- Weather market history on Polymarket US: highs since Apr 23, 2026 for all five cities; lows since Oct 15 for Chicago and NYC only.
- **Findings from small live runs in the cloud** (Chicago, Oct 15–30, HRRR+GEFS only, bracket midpoints as truth): models err in opposite directions on lows (GEFS +3.6°F, HRRR −3.7°F). The model was overconfident ("model expected" P&L far above actual). The market was better on that small sample. Too little data to conclude anything; the real run is on the owner's PC.
- A wrong guess, corrected: GEFS's mild lows at Midway were **not** a lake grid point. GEFS really forecast ~56°F against HRRR's 47°F and an actual ~50°F. Land-sea masks were added anyway, since SFO's nearest coarse-grid point is over the bay.
- **Nov 12: first full run on the owner's PC was invalid (fixed).** Biases were fixed once from the earlier half (Apr–Jul for highs), which left RRFS/REFS, whose archives start Aug 12/Sep 9, uncorrected in the scored half, and missed seasonal drift (Chicago HRRR highs about +0.5 in summer, 2–3 too cold in October; RRFS/REFS 3–6 too cold). The model scored about as well as guessing every bracket equally (Brier ~0.9), and "model expected" was +$29k against −$520 actual. Now each model's bias is a rolling 30-day mean of its own error, using results final by the decision (D−2), and a model joins once it has 7 days at a city. The output also reports each model's remaining miss per city.
- Cloud check after the fix (highs, Sep 9–Oct 31, all five cities, bracket midpoints as truth, 23 scored days each): model Brier improved to 0.55–0.60 at CHI/LAX/MIA (market 0.46–0.54, difference "not clear"). The market was clearly better at NYC (0.78 vs 0.51) and SFO (1.03 vs 0.79). Still overconfident: 174 trades, −$408 against +$4,521 "expected". The spread fit picked x1 at three cities from only ~11 fitting days, so that choice is unreliable. A width set from recent actual misses would be sturdier. RRFS/REFS ran 2.5–5°F cold in CHI/LAX/MIA highs.
- SFO grid points: GEFS 0.5°'s nearest land point is 37.5N 122.0W (near Fremont), often 15–20°F hotter than the airport. HRRR's land point there runs about 9°F cold. RRFS/REFS track the actual highs. Consider dropping GEFS at SFO if its remaining miss stays large.
- The backtest's cache check only looked at the first station, so a later run adding cities skipped their values. Fixed: a message counts as cached only when every station has it.
- **Open:** live forecasts don't apply per-model bias corrections, only pooled ones (shared or per city). If the backtest shows large opposite per-model biases, add per-model, per-city corrections to the live config.

## Position sizing (both bots, Nov 5, 2026)

Kelly is a **target position**: each check computes the total stake Kelly wants in an outcome, subtracts what's already held (open positions), and buys only the difference, if it's at least $1. Before this change, every check re-bought a full Kelly amount, so any edge that lasted a few checks grew to the per-game or per-city-day cap ($25). Positions are held to settlement; there's no selling when an edge disappears.

## Things learned the hard way

- **Polymarket US public market data** (`gateway.polymarket.us`):
  - Order books and BBO allow about **5 requests per 15 s per IP**, then return 429 with `Retry-After: 10`. The documented 25/s is for the trading API.
  - Listings and event/market lookups aren't limited like that.
  - So both bots use listing quotes to rule out trades first, and space book requests 3 s apart.
- **International Polymarket's Gamma API** returns at most 100 events per page, whatever `limit` is asked for. Before paging was fixed, NFL games were missing.
- **Open-Meteo:** a combined multi-model ensemble request labels its keys so that none matched the model IDs (0 of 143 parsed). Each model is requested separately, where keys are `temperature_2m` / `temperature_2m_memberNN`.
- **RRFS (operational since autumn 2026):**
  - Bucket `noaa-rrfs-ops-pds`. 00/06/12/18Z runs: `rrfs.YYYYMMDD/HH/rrfs.tHHz.2dfld.3km.fNNN.conus.grib2` (f000–f084), with `TMAX` (max over the hour). Ensemble: `rrfsens.../mMMM/...2dfldnomads...` (f000–f060), with `TMP` only.
  - **Other hourly runs on AWS only have** `rrfs.tHHz.2dfld.3km.subh.fNNN.conus.grib2` (f001–f018): four `TMP:2 m` messages per file, at 15-minute steps ("255 min fcst" … "5 hour fcst"). No `TMAX`. They're fully published about 1.5–2 hours after the run starts.
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

- [ ] After about 3–4 weeks of weather data: tune `bias_f`, `spread_inflation` and model weights from `calibration`, run per city. Consider **lead-dependent weights** (for example, heavier RRFS/HRRR for same-day mornings) if the per-lead table supports it. Weights are currently fixed per model, so that would need a small config change.
- [ ] Sports: judge CLV per sport at about 100 trades with a closing line; drop any sport with CLV ≤ 0.
- [ ] Not verified yet on the owner's PC: `backfill-outcomes` beyond about a week (the IEM archive path).
- [ ] Possible later: NOAA NBM probabilistic guidance (reusing the RRFS GRIB code); a time-lagged HRRR ensemble; switching the web app's browsing and orders to Polymarket US.
- [ ] Before going live, or before raising the daily caps: add an **open-position limit** (total cost of unsettled positions ≤ bankroll, or ≤ the real account balance from Polymarket US). Today the bankroll is a fixed sizing number only, and nothing tracks capital left. With weather alone, at $100/day on a $200 bankroll, the daily cap keeps open positions around $150–200, since bets on tomorrow settle about two days later. A higher daily cap, or betting further ahead, would let open positions exceed the bankroll. Also optional: a bankroll that follows settled P&L. The owner deferred both until deciding to go live, and is undecided about the sports bot, so it's weather-only for now.
- [ ] Real trading on Polymarket US: not built. It would need API keys from polymarket.us/developer (Ed25519 signing, `cryptography` package) and an explicit decision by the owner.
