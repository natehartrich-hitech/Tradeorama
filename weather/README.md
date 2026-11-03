# wxbrackets

Probabilities for daily high-temperature bracket markets (Kalshi-style "≤45 / 46-47 / … / ≥56"), built from weather-model ensembles and settled against the NWS Daily Climate Report. It only fetches data and computes probabilities. It never places trades.

## Setup

Requires Python 3.11+.

```bash
cd weather
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
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
```

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
2. **Member maxima.** For each ensemble member, the bot takes the highest hourly 2 m temperature inside that window, including both boundary hours. Members with fewer than `min_valid_hours` values in the window are dropped.
3. **Pooling.** Each model gets a weight (default equal), split evenly across its members. So ECMWF's 51 members don't outvote GEM's 21 just by number.
4. **Smoothing.** Each member max is moved away from the pooled mean by `spread_inflation` (raw ensembles are usually too narrow), then shifted by `bias_f`. A weighted Gaussian kernel is placed on each one. The kernel width is `bandwidth_f`, or a weighted Silverman rule if that's set to `auto`.
5. **Rounding.** The NWS reports whole °F, so a bracket "46-47" is the probability the true high falls in [45.5, 47.5).
6. **Same day.** If the date is today's climate day, the distribution is cut off below the highest temperature observed so far, minus `obs_tolerance_f`, and renormalised.

## Storage

All runs are stored in SQLite (`database` in the config):

| Table | Contents |
|---|---|
| `forecast_runs` | One row per run: time, station, target date, lead hours, settings used, pooled mean, distribution mean, observed max used, NWS forecast high |
| `forecast_members` | Every member's daily max and weight, per run (timestamp, station, target date, model, member, max) |
| `bracket_probs` | Each bracket's probability, per run |
| `outcomes` | Official CLI highs by station and date. A final report always replaces a same-day partial. |

`calibration` joins runs to final outcomes and reports:
- the multi-category Brier score (the sum over brackets of (p − hit)², averaged over runs; 0 is perfect)
- a reliability table of bracket probabilities against how often those brackets hit
- the mean bias and mean absolute error of the raw pooled ensemble mean against the official high, plus the NWS point forecast's bias for comparison

Use `--min-lead` and `--max-lead` to score day-ahead and same-day runs separately. Once there are a few weeks of data, set `bias_f` from the reported bias, and adjust `spread_inflation` until the reliability table lines up.

## Data sources and their limits

- **Open-Meteo Ensemble API** (`ensemble-api.open-meteo.com/v1/ensemble`): hourly `temperature_2m` for each member, in °F with `timeformat=unixtime`.
  - The free tier is for **non-commercial use only**. For commercial use, set `open_meteo.base_url` to the customer endpoint and `open_meteo.api_key`.
  - Model IDs and member counts change over time; `check-models` verifies them. Each model covers a different number of days ahead (ICON's ensemble is the shortest), and members that don't cover the date are dropped.
  - The control run's key is `temperature_2m`, and members are `temperature_2m_memberNN`. With several models, each key also carries the model ID. The parser accepts the model ID before or after the member number.
  - Hourly values can miss a peak between hours. That's one reason for `bias_f`.
- **NWS API** (`api.weather.gov`): the point forecast high (daytime period) and station observations for today's running max. Hourly and 5-minute observations can miss the true peak, so the official high can come in a degree or so above the observed running max. `obs_tolerance_f` allows for rounding in the other direction. Observations reported in °C are converted to °F.
- **NWS Daily Climate Report (CLI)**: the settlement source. The report issued after midnight is final for the previous day. Same-day reports ("VALID TODAY AS OF …") are kept only when nothing final exists, and are flagged.
  - `api.weather.gov/products/types/CLI/locations/{loc}` only keeps recent products, roughly the last week.
  - Older reports come from the Iowa Environmental Mesonet's AFOS text archive (`mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py`). That's a free academic service, so requests are kept small and spaced out.
  - The report can be corrected after issue, and the latest final issue wins.
  - Exchanges sometimes use specific rules for corrections and timing, so check the contract.

## Not verified against live services yet

This was built in an environment that couldn't reach Open-Meteo, api.weather.gov or the IEM. The request formats follow the published documentation, but the test fixtures in `tests/fixtures/` are **hand-made in the documented formats, not captured responses**. Before trusting the output:

1. Run `check-models --dump raw/` and confirm each model's member count. Fix `models[].id` in the config if any fail.
2. Run `observe` and `backfill-outcomes --days 7` and compare a few highs with the CLI reports on weather.gov.
3. Optionally, replace the fixtures with real captures (`raw/*.json`, and a CLI product saved from weather.gov). Then rerun `pytest`.

## Not included yet

NOAA NBM probabilistic guidance (via Herbie and AWS GRIB2) is the planned stretch goal. It needs the `herbie-data` package and its GRIB dependencies, which aren't in the dependency list, so it's left out until you approve adding them.

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
- an end-to-end run of the commands against a fake network built from the fixtures
