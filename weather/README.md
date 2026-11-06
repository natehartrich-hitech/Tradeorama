# wxbrackets

Probabilities for daily **high and low** temperature bracket markets ("≤45 / 46-47 / … / ≥56"), built from weather-model ensembles and settled against the NWS Daily Climate Report. Both **Polymarket US** and Kalshi settle their daily-high markets on that report. The project reads Polymarket US's markets, compares them with the model, and can **paper trade** them. It never places real orders.

## Setup

Requires Python 3.11+.

```bash
cd weather
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # includes eccodes, which reads NOAA's GRIB2 files
cp config.example.yaml config.yaml # Windows: copy config.example.yaml config.yaml
```

In `config.yaml`, set `nws.user_agent` to something that identifies you, including contact info. The NWS asks every client to do this, and the commands refuse to run with the placeholder.

Then check that the configured ensemble models return what's expected:

```bash
python -m wxbrackets check-models --station CHI
```

**Before relying on any number, confirm which station the market settles on.** Read the contract rules on the exchange. `CHI` is set to Chicago Midway (KMDW, report `CLIMDW`), and `ORD` (O'Hare) is included too. Either station can be changed, or new ones added, in `config.yaml`.

## Commands

```bash
python -m wxbrackets forecast --station CHI --date 2026-11-04 --brackets 45,47,49,51,53,55
python -m wxbrackets observe --station CHI
python -m wxbrackets backfill-outcomes --station CHI --days 30
python -m wxbrackets calibration --station CHI
python -m wxbrackets check-models --station CHI --dump raw/
python -m wxbrackets forecast --station CHI --date 2026-11-04 --kind low   # the day's low instead
python -m wxbrackets calibration --station CHI --kind low
python -m wxbrackets paper --dry                # what it would buy right now, records nothing
python -m wxbrackets paper --every 30           # paper trade every 30 minutes (Ctrl+C to stop)
python -m wxbrackets paper-report
```

### Daily lows

Everything works for the day's **low** as well, with `--kind low` on `forecast`, `calibration` and `paper-report`. `paper` trades both by default (`paper.kinds`).
- **Settlement:** the low is the CLI report's MINIMUM line, for the same standard-time climate day. `backfill-outcomes` stores both values.
- **Models:** members give their minimum over the day. RRFS's 6-hourly runs use `TMIN` (the minimum over each hour); hourly runs use their 15-minute readings; other models use hourly snapshots, which miss brief dips, so they tend to read slightly warm for lows.
- **Same day:** the observed minimum so far is a ceiling. Each member's final low is the lower of that and its forecast for the hours left. Paper trading stops on today's low market at `paper.same_day_last_hour_low` (6 AM local standard time), because by dawn the low is usually in. An evening cold front can still undercut it, which the forecast allows for.
- **Calibration:** lows get their own bias and spread settings through an optional `distribution_low` section, since overnight temperatures have different errors. Calibration and the shadow-mode comparison run per kind.
- **Cities:** Polymarket US has low series for all five cities. In November 2026 only Chicago and New York had low events listed; the others are linked (`pmus_low_prefix`) and start working once events appear.

### Polymarket US

[Polymarket US](https://polymarket.us) lists daily-high markets for Chicago (Midway), New York (Central Park), Miami, Los Angeles and San Francisco. All settle on the NWS Daily Climate Report (see the [weather FAQ](https://docs.polymarket.us/faqs/weather-faqs)). Each config station with a `pmus_event_prefix` is linked to its event, for example `temp-mdwhigh-2026-11-04`. The data is public, from `gateway.polymarket.us`, with no API key needed.

- **`forecast`** uses that day's real brackets automatically (unless you pass `--brackets`). It shows each bracket's market bid and ask, and the edge per contract for buying Yes or No after the taker fee (0.0695 × p × (1 − p), read from each market). The quotes are saved with the run.
- **`paper`** forecasts today and tomorrow for every linked city. Where buying Yes or No beats the market by at least `paper.min_edge` after fees, it records a paper buy:
  - It walks the real order book, and stops at any price where the edge runs out.
  - Bet size is quarter-Kelly on `paper.bankroll_usd`, as the **total** to hold in that bracket and side. Hourly reruns only buy what isn't held yet: nothing for an unchanged edge, a top-up if it grows. It's capped per city-day and per day, and positions are held to settlement, never sold early.
  - It stops trading `paper.stop_hours_before_end` hours before the climate day ends. For **today's** market, it stops trading at `paper.same_day_last_hour` (noon local standard time by default), and doesn't trade at all without today's observations. By midday the high is mostly set, the NWS observation feed can lag by up to an hour, and other traders watch real-time data, so late same-day "edges" are usually stale information on our side.
  - After the cutoff it keeps **forecasting and logging prices** for today without trading. `calibration` reports those untraded forecasts ("today (shadow)") separately, so if the model turns out to beat the market there, the cutoff can be moved later.
  - Finished markets are settled from Polymarket US's own results. Buying No means taking the Yes bids, at 1 − bid.
- **`calibration`** adds a **model vs. market** Brier comparison on the runs where prices were saved. The market's probabilities are each bracket's mid price, scaled to sum to 100%. This comparison is also split by lead time. The "today (shadow)" row covers untraded same-day forecasts and decides whether the cutoff should move. Only a model that scores better than the market has an edge worth paper trading.

`--brackets` takes the upper edge of each bracket, inclusive: `45,47,49` means ≤45, 46-47, 48-49, ≥50. Without it, `default_brackets` from the config is used.

### Example `forecast` output

This example was produced from the test fixtures (a 5-member toy ensemble), not live data:

```
Chicago Midway (KMDW)  high for 2026-11-03
Climate day (local standard time): 11-03 00:00 CST to 11-04 00:00 CST  |  lead -8.7 h

Models:
  ECMWF IFS 0.25° ensemble           3 of 51 members  mean max 56.0°F  weight 1
  GFS ensemble 0.25°                 2 of 31 members  mean max 53.0°F  weight 1

Pooled ensemble mean:   54.5°F
Distribution mean:      54.5°F  (bias +0.0, spread x1.3, kernel 1.50°F)
NWS point forecast:     60°F

  bracket    prob  fair ¢
      ≤51   15.8%    15.8
    52-53   20.5%    20.5
    54-55   25.7%    25.7
    56-57   23.4%    23.4
      ≥58   14.6%    14.6
    total  100.0%

Saved as run 1 in wxbrackets.db
```

## How the probabilities are built

1. **Climate day.** The official high covers midnight to midnight **local standard time**, even during daylight saving time. In summer, Chicago's climate day runs from 1:00 AM to 1:00 AM on the clock. Ensemble times are requested as Unix timestamps and converted using the zone's standard offset (`climate_day.py`).
2. **Member maxima.** For each ensemble member, the bot takes the highest hourly 2 m temperature inside that window, including both boundary hours. A member is dropped if it has fewer than `min_valid_hours` values in the window, or if its data doesn't reach within `edge_tolerance_hours` of both ends of the day. A run that stops mid-afternoon would otherwise report a high that's too low.
3. **Pooling.** Each model gets a weight (default equal), split evenly across its members. So ECMWF's 51 members don't outvote GEM's 21 just by number. A deterministic model like HRRR counts as one member that carries its model's whole weight, so it starts at 0.5 rather than 1.
4. **Smoothing.** Each member max is moved away from the pooled mean by `spread_inflation` (raw ensembles are usually too narrow), then shifted by `bias_f`. A weighted Gaussian kernel is placed on each one. The kernel width is `bandwidth_f`, or a weighted Silverman rule if that's set to `auto`.
5. **Rounding.** The NWS reports whole °F, so a bracket "46-47" is the probability the true high falls in [45.5, 47.5).
6. **Same day.** If the date is today's climate day, the bot first fetches the highest temperature observed so far. The models then only need to cover the hours left: each member's final high is the larger of the observed high and its forecast for the rest of the day. Once the afternoon has passed, the remaining hours are cool, so the distribution collapses onto the observed high. Members pinned there get a narrow kernel (`obs_tolerance_f`, the typical gap between hourly observations and the official high) instead of the models' spread. Without today's observations, the whole-day forecast is used, and `paper` won't trade today's market.

## Storage

Tables added for Polymarket US: `market_prices` (the quotes captured with each run) and `paper_fills` (paper trades and their payouts).


All runs are stored in SQLite (`database` in the config):

| Table | Contents |
|---|---|
| `forecast_runs` | One row per run: time, station, target date, lead hours, settings used, pooled mean, distribution mean, observed max used, NWS forecast high |
| `forecast_members` | Every member's daily max and weight, per run (timestamp, station, target date, model, member, max) |
| `bracket_probs` | Each bracket's probability, per run |
| `outcomes` | Official CLI highs by station and date. A final report always replaces a same-day partial. |

`calibration --station X --kind high|low` joins runs to final outcomes for that city and kind, and reports:
- the multi-category Brier score (the sum over brackets of (p − hit)², averaged over runs; 0 is perfect)
- a reliability table of bracket probabilities against how often those brackets hit
- the mean bias and mean absolute error of the raw pooled ensemble mean against the official high, plus the NWS point forecast's bias for comparison
- each model's own bias and mean absolute error, overall and split by lead time (today before the same-day cutoff, "today (shadow)" after it, next day, 2+ days), to guide model weights at each distance from the day. Same-day errors are of each model's final high (observed so far combined with its remaining-hours forecast).

Use `--min-lead` and `--max-lead` to score day-ahead and same-day runs separately.

### Per-city calibration

Each place has its own quirks: Midway's urban heat and lake breezes, Central Park's trees, Miami's sea breeze, and the marine layer and fog at LAX and SFO. So any station can override the shared settings with its own `distribution` (highs) and `distribution_low` (lows) sections, and its own `weights` / `weights_low`. Settings resolve in layers: `distribution`, then `distribution_low` for lows, then the city's own section. A weight of 0 switches a model off for that city.

`calibration` prints the correction in use for that city and kind, and a ready-to-paste section with the suggested `bias_f`. Until a city has about **30 settled days** for that kind, it warns that the number mostly reflects noise, since day-to-day errors are 2–3°F. Start shared, and give a city its own settings once its bias clearly differs from the others. Then adjust `spread_inflation` until the reliability table lines up.

## Data sources and their limits

- **Open-Meteo Ensemble API** (`ensemble-api.open-meteo.com/v1/ensemble`): hourly `temperature_2m` for each member, in °F with `timeformat=unixtime`.
  - The free tier is for **non-commercial use only**. For commercial use, set `open_meteo.base_url` to the customer endpoint and `open_meteo.api_key`.
  - Model IDs and member counts change over time; `check-models` verifies them. Each model covers a different number of days ahead (ICON's ensemble is the shortest), and members that don't cover the date are dropped.
  - Each model is requested on its own. The control run's key is then `temperature_2m`, and members are `temperature_2m_memberNN` (checked live in November 2026). A combined multi-model request labels its keys in a way that didn't match the model IDs, so it isn't used, and any keys the parser can't place are reported as a warning rather than dropped silently.
  - Hourly values can miss a peak between hours. That's one reason for `bias_f`.
- **NOAA HRRR via Open-Meteo** (`api.open-meteo.com/v1/forecast`, model `ncep_hrrr_conus`): the 3 km High-Resolution Rapid Refresh, updated hourly. It's set as `source: forecast` because it's a single deterministic run, not an ensemble.
  - Runs reach 18 hours ahead, or 48 hours for the 00/06/12/18 UTC runs. So HRRR usually covers today and sometimes tomorrow. Runs that don't cover the whole climate day are skipped automatically.
  - Only the latest run is used. Older runs aren't combined into a time-lagged ensemble.
  - Covers the contiguous US only.
- **NOAA RRFS and REFS** (bucket `noaa-rrfs-ops-pds` on AWS): NOAA's new 3 km Rapid Refresh Forecast System, read straight from NOAA's files.
  - **RRFS** is the deterministic run. Its files include `TMAX`, the maximum 2 m temperature over each hour, which catches peaks between hours. It runs every hour, and the 00/06/12/18Z runs reach 84 hours ahead.
  - **REFS** is its 5-member ensemble (members m001–m005), run at 00/06/12/18Z and reaching 60 hours ahead. Its files only have hourly temperature (`TMP`).
  - For each hour of the climate day, the bot reads the `.idx` index file and downloads just the one 2 m temperature message (about 1.7 MB) with an HTTP Range request. `eccodes` then decodes the value at the grid point nearest the station; for Midway, that point is 1.5 km away.
  - A forecast for a future day uses the newest 00/06/12/18Z run that starts no later than the climate day and has published its last needed hour. On a busy morning, that may be the run before the latest.
  - For **the rest of today**, RRFS uses the newest published **hourly** run. Those runs only publish 15-minute temperatures (`2dfld.3km.subh`, 4 steps per file, 18 hours ahead), so peaks between hours still count. Any evening hours past an hourly run's 18-hour reach come from the newest 6-hourly run. Hourly runs are published about 1.5–2 hours after they start. REFS uses its newest 6-hourly run that reaches the end of the day.
  - Data volume on the first run of a cycle: RRFS about 24 messages (~40 MB), REFS about 125 (~210 MB). `paper` reads every city's value from each downloaded message, so the five cities share one download instead of five. Values are cached in the database (`grib_points` table), so repeat forecasts from the same run download nothing. Set `weight: 0` on a model to skip it.
  - The file layout was checked against the live bucket in November 2026, and RRFS was test-run against it: it gave Midway a 61.1°F high for Nov 4 from the 06Z Nov 3 run. RRFS only went operational in autumn 2026, so NOAA may still change file names. The URL templates are in `wxbrackets/rrfs.py`, and the bucket is `rrfs.base_url` in the config.
- **NWS API** (`api.weather.gov`): the point forecast high (daytime period) and station observations for today's running max. Hourly and 5-minute observations can miss the true peak, so the official high can come in a degree or so above the observed running max. `obs_tolerance_f` allows for rounding in the other direction. Observations reported in °C are converted to °F.
- **NWS Daily Climate Report (CLI)**: the settlement source. The report issued after midnight is final for the previous day. Same-day reports ("VALID TODAY AS OF …") are kept only when nothing final exists, and are flagged.
  - `api.weather.gov/products/types/CLI/locations/{loc}` only keeps recent products, roughly the last week.
  - Older reports come from the Iowa Environmental Mesonet's AFOS text archive (`mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py`). That's a free academic service, so requests are kept small and spaced out.
  - The report can be corrected after issue, and the latest final issue wins.
  - Exchanges sometimes use specific rules for corrections and timing, so check the contract.

## Not verified against live services yet

This was built in an environment that couldn't reach Open-Meteo, api.weather.gov or the IEM. (NOAA's RRFS bucket was reachable, and RRFS/REFS was tested against it.) The request formats follow the published documentation, but the test fixtures in `tests/fixtures/` are **hand-made in the documented formats, not captured responses**. Before trusting the output:

1. Run `check-models --dump raw/` and confirm each model's member count. Fix `models[].id` in the config if any fail.
2. Run `observe` and `backfill-outcomes --days 7` and compare a few highs with the CLI reports on weather.gov.
3. Optionally, replace the fixtures with real captures (`raw/*.json`, and a CLI product saved from weather.gov). Then rerun `pytest`.

## Not included yet

**NOAA NBM** probabilistic guidance is still the planned stretch goal. It can reuse the RRFS download-and-decode code.

## Tests

```bash
pytest
```

The tests cover:
- member-key parsing
- the standard-time climate-day window (including DST and the transition days)
- bracket probability math and rounding
- CLI report parsing (final, same-day, missing, archive splitting)
- HTTP retries
- RRFS index parsing (real `.idx` files from the bucket), forecast-hour and run selection, GRIB decoding, caching, and fallback to the previous run
- an end-to-end run of the commands against a fake network built from the fixtures
