# Aircraft activity (observed low overflights)

Per-address counts of low aircraft overflights from public ADS-B traces
(adsb.lol `globe_history`, ODbL 1.0), 14 Australian city regions, 12 months
(2025-11-27 .. 2026-11-26, 359 valid local days). Built by
`scripts/aircraft_activity/` (see its README for every definition). This
service only reads the finished grids.

## Switches

| env | default | effect |
|---|---|---|
| `AIRCRAFT_ACTIVITY_DIR` | unset | data directory. Unset or missing: the three endpoints answer 503 `{"status":"unavailable"}`; everything else is unaffected |
| `AIRCRAFT_ACTIVITY_MAX_CITIES` | 2 | in-process LRU of decompressed cities (only used when the directory has not been unpacked) |
| `NOISE_AIRCRAFT_ACTIVITY` | `0` | `0` off: noise output byte-identical to before. `show`: noise results gain the `aircraft_activity` block, no number moves. `estimate` (or `1`): also fills the aircraft term from fixed-wing counts where no official ANEF contour applies |
| `AIRCRAFT_ACTIVITY_API_EXPORT` | `0` | `/scores` (the batch the licensed Property API is built from) drops `noise.aircraft_activity` unless this is `1`. `/scores/noise` (map panel) is not gated |

Turning `NOISE_AIRCRAFT_ACTIVITY` on changes `_CONFIG_SIG` (result cache key and
the `api/stamp.py` model stamp, so DA Leads' per-parcel cache flushes once);
`estimate` also changes `NOISE_MODEL_VERSION` (`-aa1`), so precomputed overlay
grids fail safe to live compute until re-baked.

## Memory

Decompressed, the 14 cities are ~590 MB (the 24-hour array is ~70%). Nothing
is loaded at startup. Run once per data drop:

    .venv/bin/python scripts/aircraft_activity_unpack.py --dir $AIRCRAFT_ACTIVITY_DIR

which writes `unpacked/<city>/*.npy` (~640 MB disk); the store then mmaps them
and a query touches a few pages (page cache shared by both gunicorn workers).
Without it each worker decompresses up to `AIRCRAFT_ACTIVITY_MAX_CITIES`
cities (~45 MB each) into memory.

## Endpoints (`/scores/aircraft-activity/...`)

Shapes follow the limon-ops prototype (`labs/aircraft-activity`, `/api/*`),
with coverage enforced and attribution added.

- `GET /scores/aircraft-activity/cities` -> `{status, cities: [describe], errors, attribution, licence, licence_url}`.
  Each city: `city, key, date_start, date_end, n_days, n_weekdays, n_weekend_days, missing_days_count,
  radius_m, ceiling_ft_agl, timezone, source, fixture, coverage, coverage_note, bounds [w,s,e,n], centre,
  cell_size_deg, shape, airports, metrics, scales, has_hourly, helipads, message?`.
  `insufficient`: `metrics` and `helipads` empty, `airports` empty. `large_aircraft_only`: `metrics` = `["jet_per_day"]`, no helipads, no hourly.
  (The prototype's `data_dir` is not returned: it is a server path.)
  `?helipads=false` returns `helipads: null` plus `helipad_count` (the 12-month release has 3,081 sites; fetch them per city below).
  `airports[]`: `icao, name, lat, lon, official_month` (Airservices months compared),
  `fixed_wing_visible_ratio_low/high`, `heli_visible_ratio_low/high` (our counted movements / Airservices
  published movements; low = 100 ft threshold, high = 250 ft), `observed_movements_12m`, `observed_movements_per_day`
  (null: not in the data contract yet). Airports are listed for every coverage label.
- `GET /scores/aircraft-activity/helipads?city=&bbox=w,s,e,n&min_movements=0` ->
  `{status, city, period, night_hours_local, total_in_city, count, helipads, attribution, licence, message?}`;
  each site `{name (null when unnamed), kind, is_hospital, lat, lon, movements, arrivals, departures, night_movements}`
  (movements over the period). Empty for `insufficient` and `large_aircraft_only` cities (helicopters not reliably seen).
- `GET /scores/aircraft-activity/lookup?lat=&lng=` -> `{status, query, city, cell, metrics, hourly, quarters,
  helipads, helipad_radius_km, nearest_airport, attribution, licence, message?, undercounted?}`.
  `status` is the city's coverage label, or `not_covered` (HTTP 200, with `cities`).
  `quarters.passes_per_day` / `quarters.jet_per_day` = `{min, max, n_quarters, by_quarter}` across q1..q4.
  `city.missing_days_count` = days dropped for incomplete archives.
  `insufficient`: `metrics`, `hourly`, `quarters` null. `large_aircraft_only`: `metrics` only `jet_per_day`, `hourly` null, `undercounted: ["light","helicopter"]`.
- `GET /scores/aircraft-activity/grid?city=&metric=&bbox=w,s,e,n&max=180` -> prototype shape
  `{status, city, metric, stride, rows, cols, bounds, scale, order, values, attribution, licence}`.
  `insufficient` city: 200 with empty `values`. Metric not allowed by coverage: 400.

## `noise.aircraft_activity` (stable API block)

Same keys in every state; numbers are null when not given.

| field | unit / type | meaning |
|---|---|---|
| `status` | `ok` \| `large_aircraft_only` \| `insufficient` \| `not_covered` \| `unavailable` \| `error` | |
| `message` | string \| null | human sentence for every non-ok state |
| `affects_score` | `false` always | this block never moves a dB or a score |
| `flights_per_day` | flights/day | all aircraft, within `radius_m` sideways and at or below `ceiling_ft_agl` |
| `flights_per_night` | flights/night | 22:00-06:00 local |
| `by_type_per_day` | `{jet, light, helicopter}` flights/day | `large_aircraft_only`: only `jet` |
| `helicopter_per_night` | flights/night | |
| `typical_height_ft_agl` | ft | median height of counted passes (null for `large_aircraft_only`) |
| `quarterly_flights_per_day` | `{basis: all\|jet, min, max, quarters}` | range of the daily rate across the four calendar quarters |
| `undercounted` | list | `["light","helicopter"]` for `large_aircraft_only` |
| `period` | `{date_start, date_end, valid_days, missing_days}` | |
| `definition` | `{radius_m, ceiling_ft_agl, night_hours_local, cell_size_m, counting}` | |
| `coverage`, `coverage_note`, `city` | | receiver coverage label and its evidence |
| `source`, `licence`, `licence_url`, `attribution` | | `adsb.lol`, `ODbL-1.0` |

The block is built from the overflight data and the coordinate only. Nothing in
it depends on whether an official ANEF contour applies, so it is the same on
both sides of a contour edge (tested) and cannot be used to trace one.

## `noise.aircraft_estimate` (only with `estimate`, only where used)

Present only when the aircraft term of the level came from overflights:
`{lden_db, leq_db, leq_day_db, leq_evening_db, leq_night_db, classes, method, basis, source, licence, licence_url, attribution}`.
`aircraft_db` is then set to `leq_db` and `dominant_source` may read `aircraft`.
A sentence is appended to `confidence_note`.

Rules: official contour hit -> official term, no estimate. `exclude_aircraft`
-> no estimate. Coverage `insufficient` -> nothing; `large_aircraft_only` ->
jets only. Helicopters never become dB. Aircraft Lden below 45 dB is not mixed.

## Estimate method

Every counted pass is an event with SEL = SEL(1,000 ft) by class (jet 86, light
74 dBA) - 20 log10(d / 305 m), averaged over a sideways offset spread evenly
from 0 to 1,000 m at the cell's median height; energy summed per hour with
Lden evening +5 / night +10 weights, divided by 86,400 s. The same energy per
period gives day (07-19), evening (19-23) and night (23-07) Leqs, which is how
the estimate enters the engine's period sums (the ANEF term, by contrast, is
one Leq in all three periods), so a daytime training airfield does not show
up in `leq_night_db`. `aircraft_db` is the estimate's 24 h Leq. Conservative choices, each biasing low: even sideways
spread, nothing above 3,000 ft counted, low-end SELs (lowered 2 dB after the
check below).

Check against official ANEF (`scripts/aircraft_activity_calibrate.py`, 12-month
release, SEL 86/74). Median estimate per band vs ANEF + 35 (the usual ANEF ~
DNL - 35 relation):

| city | ANEF 20 | 25 | 30 | 35 |
|---|---|---|---|---|
| Perth (all cells in band) | 55.1 (+0.1) | 58.5 (-1.5) | 62.4 (-2.6) | 67.3 (-2.7) |
| Brisbane, jets only | 53.8 (-1.2) | 57.7 (-2.3) | 60.3 (-4.7) | 64.5 (-5.5) |
| Melbourne MAEO (all cells) | 41.5 (-13.5) | 40.0 (-20.0) | | |
| Melbourne MAEO, cells with >= 20 jets/day | ~55 | ~57 | | |

Against the dB this engine already uses for each ANEF band (35 + penalty, flat)
the estimate is 6-12 dB lower everywhere. Melbourne's MAEO is drawn for the
ultimate airport layout (future runways), so most of its area has little
current traffic; under today's corridors the estimate lands near ANEF + 35.
ANEF is a forecast of future capacity, so current traffic should read at or
below it.

## Known limits

- `median_agl_ft` is the median of a bimodal distribution in airport corridors
  (arrivals ~700-800 ft, departures ~1,600-1,900 ft at Keilor Park). It follows
  the season's runway mix: summer months there have medians of 1,600-1,900 ft,
  winter 700 ft, the 12-month value 806 ft. Not a bug; a single "typical
  height" hides two populations.
- Hourly counts exist only for all aircraft together; each class is given that
  profile.
