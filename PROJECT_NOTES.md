# Project notes

Shared memory for this project: who it's for, what was decided and why, what was learned the hard way, and what's still open. The READMEs explain how to use things; this file explains the reasoning. Update it when a decision changes.

## The owner's setup

- Windows 11 PC, Chicago area, Python 3.14. Commands run in PowerShell, using `python`, not `python3`.
- **Trades on Polymarket US** (polymarket.us), the CFTC-regulated exchange for US residents. The international Polymarket site restricts US users, so it isn't the target venue.
- **Everything is paper trading.** No real orders are placed anywhere, and live trading on Polymarket US isn't built.
- The Odds API plan has 20,000 credits a month, used for Pinnacle odds.
- Progress is tracked on two checklists (artifacts in the owner's Claude account): "Tradeorama Setup Checklist" covers the app and sports bot, and "Weather Bot Checklist" covers the weather project.
- **Goals (Nov 12, 2026):**
  - The aim is a paper strategy that passes a test strict enough to risk real money: possibly thousands of dollars, so the market can't be too thin.
  - About 10 hours a week. A few hundred dollars for data is fine if a test justifies it. A cloud server is fine.
  - Open to any venue or account.
  - Works in the freight industry: nothing that could compete with the employer (see the next-project section).

## What's in the repo

| Part | Where | Status |
|---|---|---|
| Sports bot: Pinnacle +EV against Polymarket US | `tradeorama/strategy/` | Paper trading since early Nov 2026 |
| Weather model and paper trader: daily highs and lows | `weather/` (package `wxbrackets`, own venv and `config.yaml`) | Highs paper trading since Nov 5, 2026; lows added Nov 5 |
| Web app: browse markets, manual orders, bot panel | `tradeorama/app.py`, `tradeorama/static/` | Market browsing and orders still use the **international** site |
| Replay: Kalshi's past sports trades against Pinnacle | `tradeorama/replay/` (`python -m tradeorama.replay`) | Built Nov 12, 2026. NBA Feb 2026 run: **fails**; idea dropped (see next-project section) |
| Structural scan: Kalshi's favorite-longshot bias after fees | `tradeorama/scan/` (`python -m tradeorama.scan`) | Rules fixed Nov 12, 2026; first run pending on the owner's PC |

Both bots run at once. The weather bot runs as a Windows scheduled task that starts at boot with nobody signed in (`scripts\bots.ps1`; see "Running unattended" below). The sports bot runs in a terminal window: the owner chose not to start it at boot (Nov 11, 2026).

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
- **Illinois and sports contracts (checked Nov 12, 2026; changing fast, so recheck before going live):**
  - The Illinois Gaming Board sent cease-and-desist letters to Kalshi (Apr 2025), to Crypto.com and Robinhood, and to Polymarket US (QCX LLC, Jan 27, 2026).
  - SB 3019 (signed Jun 16, 2026) treats sports contracts on exchanges as "exchange wagers". It requires a $15M state license and adds a tax of 1.75% per wager (3.5% beyond 5 million wagers a year); exchanges without a license must geo-block Illinois.
  - On Oct 30, 2026 Judge Pacold (N.D. Ill.) granted Kalshi and Coinbase partial preliminary injunctions: the contracts are likely swaps under federal law. Reports disagree on whether the transaction tax is blocked too. Illinois appealed to the Seventh Circuit.
  - Unknown: whether Polymarket US and Kalshi offer sports to Illinois accounts today, and whether the tax would land on traders.
  - Taxes: from 2026, gambling losses are deductible only up to 90% of winnings. A retroactive fix passed the House Ways and Means Committee in Sept 2026, but it wasn't confirmed as law. The IRS hasn't said how prediction-market contracts are taxed. See a tax preparer before real money.

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
- **Nov 12, full rerun on the owner's PC after the fix** (official values, Apr 23–Nov 10, 91 scored days per city for highs): the market was clearly better at CHI, LAX, NYC and SFO highs; MIA was even (0.634 vs 0.610). 636 trades lost −$598 (luck 74%: consistent with no skill). "Model expected" was +$18k.
  - The model is roughly calibrated for its own accuracy, but less accurate. A calibrated 2°F-bracket forecast with error sd σ scores Brier ≈ 1 − 1/(σ√π). The market's 0.54–0.61 implies σ ≈ 1.2–1.5°F at the decision (SFO 0.74 ≈ 2.2°F). The model's 0.63–0.74 implies σ ≈ 1.5°F (MIA) to 2.2°F (CHI). SFO's model (0.94) is badly off. When a less accurate model disagrees with the market, the gap is mostly the model's own error, so "expected" P&L far above actual means the market knows more, not just that the spread is too narrow.
  - Checked for look-ahead: decision-time prices are not converged (the favorite is at 54–64% and sometimes loses) and settle only the next day.
  - Lows: 10 scored days each, and NYC lows had full market prices on only 1 day. Too few to judge. NYC lows +$720 came from 6 trades, almost certainly one longshot.
  - Weather markets' feeCoefficient is 0.095 (all 2,000+ cached markets), not 0.0695. The code reads each market's own value.
- Nov 12: added the market+model blend test to the backtest (log pool, model weight fitted on the earlier half). Cloud check (highs, Sep 9–Oct 31, 23 scored days per city, bracket truth): all-city weight 0.10. The blend's Brier was within ±0.01 of the market's everywhere except NYC (+0.025, worse). Blend trading: 8 trades, −$6. Early sign that the archive models add little beyond the market; the owner's full run decides.
- **Nov 12, blend test on the owner's full run (the verdict for highs):** all-city model weight 0.15. On 91 later days per city, blend vs market Brier differences were all within about ±0.01 and none was clear (MIA −0.009, range −0.022..+0.004, the closest). Blend trading: 126 trades, +$49, luck 28%, against +$177 expected. **Conclusion: at the evening-before decision, HRRR/GEFS/RRFS/REFS add essentially nothing the market doesn't already price.** Don't trade highs on this model.
  - Still open: lows (10 scored days each so far; CHI lows' market scored 0.995, weaker than for highs, but the sample is tiny and NYC lows had full prices on only 1 day). Same-day trading on observations (the paper bot's "today" trades) isn't covered by the backtest. Nor are NBM and ECMWF.
- SFO grid points: GEFS 0.5°'s nearest land point is 37.5N 122.0W (near Fremont), often 15–20°F hotter than the airport. HRRR's land point there runs about 9°F cold. RRFS/REFS track the actual highs. Consider dropping GEFS at SFO if its remaining miss stays large.
- The backtest's cache check only looked at the first station, so a later run adding cities skipped their values. Fixed: a message counts as cached only when every station has it.
- **Open:** live forecasts don't apply per-model bias corrections, only pooled ones (shared or per city). If the backtest shows large opposite per-model biases, add per-model, per-city corrections to the live config.

## Running unattended (added Nov 11, 2026)

- **Why:** on Nov 10, 2026 at 9:14 PM, Windows Update restarted the PC three times (MoUsoCoreWorker, then TrustedInstaller twice). It was back up by 9:22 PM, but the bots lived in terminal windows, so nothing ran for about 20 hours. That lost Nov 11's morning same-day trading window and its shadow-mode prices; the database was intact.
- **Boot-time task (weather bot only):** `scripts\bots.ps1 install` (admin PowerShell) creates "Weather bot" under `\Tradeorama\` in Task Scheduler. It starts 2 minutes after boot, runs as the owner without a stored password (S4U), with no time limit (the default 3-day limit would kill it). The `.cmd` launcher sets `PYTHONUTF8=1` (bracket labels contain "≤", which cp1252 can't encode once output goes to a file), appends to `logs\weather-bot.log` (rotated at start-up once over 5 MB), and starts the bot again 2 minutes after it exits. The owner decided the **sports bot should not start at boot**; it still runs by hand in a terminal.
- **Missed-run alerts:** both bots ping a dead man's switch (Healthchecks.io: period 1 hour, grace 30 minutes) after each pass and every 10 minutes while waiting, and post `/fail` on a crash. A check on the PC itself couldn't alert when the PC is off, which is the case that mattered. URLs: `STRATEGY_HEARTBEAT_URL` in `.env`, `heartbeat_url` in `weather/config.yaml`. Unset means no pings; `paper --dry` never pings. A heartbeat error never stops a bot.
- **The weather database moved to `F:\wxdata\wxbrackets.db`** (Nov 11, `database:` in `weather/config.yaml`). The F: drive must be present at boot for the weather task.

## Next project: choosing what to test (Nov 12, 2026)

- **Why change course:** the weather verdict says that trying to *be* the sharp price with public data loses to a market that already prices it in. Ideas more likely to work here do one of three things:
  1. borrow a sharp price from a deeper market and trade a softer venue that doesn't ban winners (the sports bot's pattern);
  2. trade a documented structural bias;
  3. trade markets whose answer is partly published before they close.
- **Shortlist, in order:**
  1. **Kalshi sports vs Pinnacle.** Kalshi is liquid (an ordinary NBA game trades millions of contracts, roughly $400k in the 6 hours before an estimated tip-off in Feb 2026), and unlike sportsbooks it doesn't limit winners. Risk: market makers price off sharp lines, so gaps may be rare, plus Illinois' legal status (above).
  2. **Kalshi structural scan.** Bürgi, Deng and Whelan found that longshots win less often than priced and that makers beat takers. Fit on older data, score on newer. It could also seed a data product. A paper test can't know queue position, so maker results are optimistic.
  3. **Kalshi TSA check-ins and EIA gas prices.** The answer is partly public before the market closes. Legally clean, but likely smaller.
- **Ruled out or parked:**
  - Kalshi weather with the same models: already answered by the backtest.
  - Economic releases: too few events a year for a verdict.
  - Pure arbitrage and speed plays: bots and co-located traders win.
  - Illinois sportsbooks: they limit winners, and FanDuel and DraftKings add 50¢ per bet in Illinois.
  - **Anything freight-related:** it could compete with the owner's employer, and Illinois' Employee Patent Act lets invention-assignment clauses cover side projects related to the employer's business. Not without a written OK.
- **First test (decided before seeing results):** replay Kalshi's NBA moneyline trades for Feb 2026 against Pinnacle's no-vig line (`tradeorama/replay`, README "Replay"). **Kill rules:**
  - fewer than 1 game-side with a gap per game day;
  - a median under $100 traded at qualifying prices per window;
  - average CLV under +1¢.

  If it passes, rerun on Jan–Apr and the NFL, then paper trade on Kalshi. Don't loosen the rules or hunt across leagues after a fail. That's how the weather model's "expected profit" fooled us.
- **Verdict, NBA Feb 2026 (owner's PC, Nov 12): fails; Kalshi sports vs Pinnacle is dropped.**
  - Coverage: all 166 games matched, 693k pregame trades, $81M.
  - Kalshi tracks Pinnacle tightly: the dollar-weighted median distance from Pinnacle's no-vig line is 1.0¢. Takers pay a median 1.0¢ *above* Pinnacle's fair price, before the ~1.75¢ fee.
  - The report found 4 windows on 3 game-sides (0.14 a day; the rule needs 1).
  - Three of the four were one game, Minnesota at Memphis on Feb 2, and they were artifacts. The Odds API's game list gave the start about 1½ hours late. Kalshi's prices show tip-off near 00:45 UTC, and the game was over by about 03:10. So in-game trades were compared with Pinnacle's in-game odds. The replay now leaves out any game whose Kalshi market closed too soon after the listed start (NBA 1¾ h, NFL 2½ h).
  - Without that game: one window, a single $1 trade, in a month. Two of the three kill rules fail.
  - Not tested: lags shorter than the 30-minute snapshots. 30% of trades fell in intervals where Pinnacle moved. Catching those is the speed game, already ruled out.
  - Per the rule set in advance, no hunting in other months or the NFL.
  - Implications:
    - The deepest US venue prices NBA moneylines as sharply as Pinnacle, so expect the Polymarket US bot's CLV to be thin too; its own test continues.
    - The ~1¢ that takers pay above fair is the makers' gross margin, which is shortlist item 2's question.
- Smoke test of the pipeline on real Kalshi data, with a stand-in for the Odds API: all 166 Feb 2026 games matched, about 640,000 pregame trades in about 1½ minutes, a ~40–50 MB cache, and a 5-second report. The stand-in's flat 50/50 lines showed why the kill rules alone aren't enough: against a wrong reference, every window "beat the close" by +8¢, while the luck check against real results said 42% (no skill). If the real run passes, check the largest-gap rows and the luck check too.

## Structural scan: rules fixed before the first run (Nov 12, 2026)

Shortlist item 2: `tradeorama/scan` (`python -m tradeorama.scan fetch`, then `report`). Kalshi public data only, analysis only. These rules were committed before any real data was downloaded. The constants live in `tradeorama/scan/analysis.py`, so they can't be changed from the command line.

- **Question:** do contracts priced 85–97¢ (the opposite side of overpriced longshots) win more often than their price, by enough to beat fees?
- **Universe:**
  - Settled Kalshi markets whose reference time falls between Oct 1, 2025 and Aug 31, 2026 (the archive ends Sept 13, 2026).
  - The top 25 series by all-time volume in each category, plus the top 30 sports series. A series needs at least 1M contracts traded. Combos ("Exotics") are excluded.
  - **Reference time, fixed when the market is created (revised before any data, Nov 12):**
    - Fixed-close markets: Kalshi's `early_close_condition` is empty, or says trading ends "regardless of any data releases or events" (weather). For these the reference is the close. Examples: index levels, the Fed decision, gas prices, oil, Rotten Tomatoes.
    - Markets that close early when their event happens ("…close and expire early if the event occurs": mentions, nominees, shutdowns, ads), and all sports: the reference is the scheduled `expected_expiration_time`. Kalshi doesn't move it when a market closes early. The Fed chair nominee market closed Mar 2026 with its expected expiration still Jan 2029.
    - Why: measuring from the actual close of such markets would leak the outcome.
    - The first version required the event's `strike_date`. Kalshi only fills that in from mid-Aug 2026, which kept just two weeks of data, so it was replaced. This was found from event counts in the first minutes of the download, before any prices or results were looked at.
  - Sports get their own verdict, because of Illinois.
- **Sample:** deterministic, from a hash of each ticker, over each series' listing from Oct 2025 to now. About 3 events per series per month and at most 3 markets per event. That was 8 at first; it was cut before any data was analysed, because unauthenticated Kalshi reads from the cloud sustain only about 2–4 requests a second. Markets within one event share an outcome, so the number of events and days drives the test's power. Markets carry an inverse-sampling weight so volumes scale back up. Series whose markets open less than about an hour before their time (the 15-minute crypto series) can't be tested and are skipped. These sizes were set for download time, before any returns were seen.
- **Bets:** at 24, 6 and 1 hour before the reference time, the market must be open, with a two-sided quote from the hourly candle that ended in the hour up to then. A side whose ask is 85–97¢ is bought: YES at the YES ask, or NO at 1 − the YES bid. The stake is $100.
  - **Taker:** pays the ask plus the fee: 0.07 × the series' (or the event's override) multiplier × p × (1 − p), rounded up per 100-contract order.
  - **Maker:** bids 1¢ under the ask. It's filled only if a trade printed at or through the bid in the next hour, or strictly through it when the bid joins an existing level. It pays the maker fee (0.0175 × multiplier) only on series with maker fees.
  - Current fee settings are used. Kalshi's fee history isn't applied.
- **Fit and holdout:** Oct–Apr is for choosing; May–Aug is scored once with the choices frozen.
  - **(a) Taker, all groups pooled:** the decision time with the best fit-period return.
  - **(b) Taker, best groups:** up to 3 (group, decision time) pairs. Each needs at least 200 fit-period bets and at least +1¢ per contract on fit. A group is a category, or a series for sports.
  - **(c) Maker:** chosen the same way as (b), on maker returns.
- **Kill rules on the holdout.** A hypothesis fails if any one fails:
  - under +1¢ per contract after fees;
  - a luck check of 5% or more (day-resampling bootstrap, with no-edge returns centered at zero);
  - positive in fewer than 3 of the 4 months;
  - under $2,000 a week traded at qualifying prices (the decision hour's volume × price, scaled up by the sampling weights).
- **The idea passes if any hypothesis passes.** With three tries, a narrow pass is weak evidence. A pass leads to live paper trading against the real order book, not to money.
- **Known optimism:** maker fills ignore the queue beyond the join rule, and the hour's volume is market volume, not volume you would get. The tail risk of buying favorites (one loss wipes out 10–35 wins) shows up only if the holdout contains it. The report prints the worst day.

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
- **Redirecting Python output on Windows** (`> file`) uses cp1252, which can't encode − or ≥, so it can crash. Windows PowerShell also re-decodes the output, garbling ¢ and –. The replay and scan reports take `--out FILE` and write UTF-8 themselves; stdout falls back to `?` instead of crashing.
- **The Odds API puts the key in the URL.** httpx's INFO logging printed it once, so the key was rotated and httpx logging is now set to WARNING.
- **Kalshi public market data** (checked live Nov 12, 2026):
  - Base `https://external-api.kalshi.com/trade-api/v2` (the official SDK's host). `api.elections.kalshi.com` serves the same data. No key is needed.
  - Data is split into a live tier and an archive. `GET /historical/cutoff` gives the boundary: Sept 13, 2026 for settled markets and trades. Anything older is only served by `/historical/markets` (series filter, no time filter) and `/historical/trades` (`ticker`, `min_ts`, `max_ts`). Pages hold up to 1,000 items, and trades come newest first.
  - Fixed-point strings: `yes_price_dollars` "0.4500", `count_fp` "31.00". Use `taker_outcome_side` (yes/no). `taker_side` is deprecated. `is_block_trade` marks off-book trades.
  - Game series: `KXNBAGAME` and `KXNFLGAME`, `fee_type` `quadratic_with_maker_fees`, multiplier 1 (taker fee 0.07 × p × (1 − p)). Each event (e.g. `KXNBAGAME-26FEB28NOPUTA`, US Eastern date) has two markets, one per team, with standard team codes. The NFL Rams appear as both `LA` and `LAR`, and Jacksonville as `JAC`. Voided games settle as `scalar`.
  - Robinhood's Chicago weather markets settle on Weather Underground, while Kalshi's and Polymarket US's settle on the NWS report. Same-looking contracts can settle differently.

## Cloud-session limits (not the owner's PC)

These sessions couldn't reach Open-Meteo, api.weather.gov, forecast.weather.gov or the IEM archive, so those paths were built from docs and verified on the owner's PC. Polymarket US (`gateway.polymarket.us`, `docs.polymarket.us`), NOAA's RRFS bucket and PyPI were reachable, and were checked live.

Since Nov 12, 2026, a personal cloud environment named "tradeorama" also allows Kalshi's two API hosts, so the Kalshi side of the replay was checked live. The Odds API isn't reachable from the cloud, and its key stays on the owner's PC, so Odds API runs happen there.

## Known limitations of the weather model

1. Not calibrated yet. `bias_f` and `spread_inflation` are starting guesses.
2. Probably runs slightly cold: hourly data misses peaks between hours, except RRFS's `TMAX`.
3. Model grid cells (3–25 km) aren't the station's exact microclimate.
4. The bias correction is a single offset, not a statistical correction per model and situation.
5. Thin markets: wide bid/ask gaps on most brackets.

## Open items

- [x] Replay of Kalshi NBA trades against Pinnacle, Feb 2026: failed the kill rules (Nov 12). Idea dropped.
- [ ] Structural scan (item 2): rules fixed (section above). **Run on the owner's PC** (`python -m tradeorama.scan fetch`, then `report`), because from the cloud Kalshi's rate limit allowed only about 1 request a second, roughly 9 hours for the sample. Apply the rules as written.

- [ ] After about 3–4 weeks of weather data: tune `bias_f`, `spread_inflation` and model weights from `calibration`, run per city. Consider **lead-dependent weights** (for example, heavier RRFS/HRRR for same-day mornings) if the per-lead table supports it. Weights are currently fixed per model, so that would need a small config change.
- [ ] Sports: judge CLV per sport at about 100 trades with a closing line; drop any sport with CLV ≤ 0.
- [ ] Not verified yet on the owner's PC: `backfill-outcomes` beyond about a week (the IEM archive path).
- [ ] Possible later: NOAA NBM probabilistic guidance (reusing the RRFS GRIB code); a time-lagged HRRR ensemble; switching the web app's browsing and orders to Polymarket US.
- [ ] Before going live, or before raising the daily caps: add an **open-position limit** (total cost of unsettled positions ≤ bankroll, or ≤ the real account balance from Polymarket US). Today the bankroll is a fixed sizing number only, and nothing tracks capital left. With weather alone, at $100/day on a $200 bankroll, the daily cap keeps open positions around $150–200, since bets on tomorrow settle about two days later. A higher daily cap, or betting further ahead, would let open positions exceed the bankroll. Also optional: a bankroll that follows settled P&L. The owner deferred both until deciding to go live, and is undecided about the sports bot, so it's weather-only for now.
- [ ] Real trading on Polymarket US: not built. It would need API keys from polymarket.us/developer (Ed25519 signing, `cryptography` package) and an explicit decision by the owner.
