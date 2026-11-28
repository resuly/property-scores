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
| `NOISE_AIRCRAFT_ACTIVITY` | `0` | `0` off: noise output byte-identical to before, with or without `exclude_aircraft`. `show`: noise results gain the `aircraft_activity` block, no number moves. `estimate` (or `1`): also fills the aircraft term from fixed-wing counts (rules below) |
| `AIRCRAFT_ACTIVITY_API_EXPORT` | `0` | `/scores` (the batch the licensed Property API is built from) drops both `noise.aircraft_activity` and `noise.aircraft_estimate` unless this is `1` (one rule for both, see "Export"). `/scores/noise` (map panel) is not gated |

Turning `NOISE_AIRCRAFT_ACTIVITY` on changes `_CONFIG_SIG` (result cache key and
the `api/stamp.py` model stamp, so DA Leads' per-parcel cache flushes once and
every parcel is recomputed on its next lookup). For `estimate` that is the
point: numbers move. `estimate` also changes `NOISE_MODEL_VERSION` (`-aa1`), so
precomputed overlay grids fail safe to live compute until re-baked.

`show` moves no number but is in `_CONFIG_SIG` as well, because it changes the
cached dict's shape (the block): without it an off process could serve a
cached show result, breaking "off is byte-identical". The cost is that turning
`show` on also flushes DA Leads' parcel cache for no number change. If that
matters, the fix is to keep `show` in the result-cache key but leave it out
of the model stamp (two signatures); not done, recorded as a suggestion.

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
  `insufficient`: `metrics` and `helipads` empty; `airports` still listed (the low observed share is the evidence for the label). `large_aircraft_only`: `metrics` = `["jet_per_day"]`, no helipads, no hourly.
  (The prototype's `data_dir` is not returned: it is a server path.)
  `?helipads=false` returns `helipads: null` plus `helipad_count` (the 12-month release has 3,081 sites in its files, 241 of them public with the named files; fetch them per city below).
  **Public landing sites** (every list and count: `cities`, `helipads`, `lookup`): a site is returned only when it is named
  or has at least `PUBLIC_MIN_MOVEMENTS_UNNAMED = 20` movements (arrivals + departures) over the period, because an
  unnamed cluster with a few touchdowns a year is a paddock or a one-off job, not a landing site; the files keep every site.
  When the naming step gives a site `name_kind: hospital`, the public `kind` is `hospital` (`is_hospital: true`). The naming step never lowers a kind;
  files without `name_kind` keep their `kind`. The naming working fields (`name_source`, `name_kind`, `name_dist_m`, `name_candidates`) are never served.
  `airports[]`: `icao, name, lat, lon, official_month` (Airservices months compared),
  `fixed_wing_visible_ratio_low/high`, `heli_visible_ratio_low/high` (our counted movements / Airservices
  published movements; low = 100 ft threshold, high = 250 ft), `observed_movements_12m`, `observed_movements_per_day`
  (null: not in the data contract yet). Airports are listed for every coverage label.
- `GET /scores/aircraft-activity/helipads?city=&bbox=w,s,e,n&min_movements=0` ->
  `{status, city, period, night_hours_local, total_in_city, count, helipads, attribution, licence, message?}`;
  each site `{name (null when unnamed), kind, is_hospital, lat, lon, movements, arrivals, departures, night_movements}`
  (movements over the period). Empty for `insufficient` and `large_aircraft_only` cities (helicopters not reliably seen).
  `total_in_city` and `count` are public sites; `min_movements` filters further on top of the public rule.
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

Rules, in order:

1. May the estimate enter a level at all (`estimate_for_level`, overflight
   data only): coverage label `ok` and estimate Lden >= 45 dB. `insufficient`
   and `large_aircraft_only` cities (Brisbane, Newcastle, Launceston and the
   `insufficient` ones) are never estimated: there light aircraft and
   helicopters are not reliably received, so a jets-only figure would read as
   an assessed, low level beside a training field it cannot see (Archerfield).
   Helicopters are counted but never become dB.
2. Caller passed `exclude_aircraft` (the official contours may not be used):
   no official source is read, and the estimate from step 1 applies on its
   own. `aircraft_excluded: true` and `aircraft_assessment: "not_assessed"`
   keep meaning "no official contour was used"; the note says the aircraft
   part is estimated, and nothing about whether a contour covers the point.
3. Otherwise official contours are read. A hit keeps the official term and no
   estimate. No hit (`no_overlay`, or `not_assessed` in the Sydney box and
   SA/TAS/ACT/NT) takes the estimate from step 1.

`exclude_aircraft` is accepted by `/scores` (where `aircraft_noise` then
answers `not_assessed` without reading any contour: a real hit there beside a
contour-blind noise result would let a consumer that strips
`noise.aircraft_db` on withheld hits, as DA Leads does, re-draw the edge),
`/scores/noise` (which then skips the precomputed grid, baked with the
official term) and
`/scores/noise/surface` (whose response adds `aircraft_estimate_nodes`, only
when non-zero). Without the noise flag on, it behaves exactly as before: road
and rail only.

## Withheld contours: who decides, and why the estimate cannot trace them

DA Leads decides which official contours it may pass on, per surface
(`web/aircraft_noise_rights.py`, read 2026-11-29): Sydney Kingsford Smith
(`syd_masterplan_2033`) and Perth Airport (`wa_slip`) are withheld on every
surface, Brisbane City Council (`qld_bcc`, Brisbane + Archerfield) on the paid
API only. This engine has no city list for that and must not grow one: the
licence decision lives with the caller, and the engine only obeys
`exclude_aircraft`. (The engine itself has held no Kingsford Smith contour
since 2026-10-22, so the Sydney box is `not_assessed` for every caller.)

Without the flag, the estimate appears outside a contour and not inside it,
and its note says "outside the official contours". Where DA Leads withholds
the contour that pattern traces the edge, even with every contour field
removed. With the flag, whether the estimate appears and how big it is are a
function of the overflight data and the coordinate only: identical on both
sides of any contour (tested by
`test_excluded_request_is_the_same_inside_and_outside_a_contour`). The
estimate is of course louder under real flight paths, which is also where
contours are drawn; that is our own measurement of the traffic, not their
dataset.

So the contract for turning `estimate` on is: DA Leads passes
`exclude_aircraft=true` on every request where it withholds a contour, on
that surface (website: Sydney box and WA; paid API: those plus the Brisbane
box; the grid already does this in its two boxes). Requests that do not
pass it keep the official-first rule, which is only ever right where the
caller may show the contour anyway. With the 12-month release the hard part
of that is WA: Perth is coverage `ok`, so without the flag the estimate
traces the Perth contour on both surfaces. The Sydney box is uniform without
the flag (the engine holds no Kingsford Smith contour, so no hit can switch
the estimate off there), and Brisbane is `large_aircraft_only`, so no
estimate applies there at all; the flag in those two only guards against a
later data release or source change. Passing it also removes the official
term from the level (see below), which moves Perth and, if passed there,
Brisbane paid-API numbers near the airports.

Two surfaces for one address do not leak by being compared. In WA and the
Sydney box both surfaces pass the flag and get the same answer. In the
Brisbane box the website does not pass it and shows the Brisbane contours
openly, while the paid API passes it; the difference between the two can
reveal only a contour that is already public on the website, and the paid
payload itself carries nothing contour-shaped, which is what the licence is
about. A caller that passes the flag also gets a level with no official
term at all, which removes the existing trace through the total level
(DA Leads strips contour fields but, by its own note, not the modelled dB).

## Export

`/scores` drops `aircraft_activity` and `aircraft_estimate` together unless
`AIRCRAFT_ACTIVITY_API_EXPORT=1`. The counts block is an extract of an ODbL
derived database; publishing it per address is Bo's call. The estimate's
numbers are our own output (company terms, section 7: scores, distances and
dB we compute carry no ODbL condition), and they reach the API anyway as
`aircraft_db`, `lden_db` and `score`. What the estimate block adds is the
per-period breakdown with the ODbL source, licence and attribution labels;
shipping that while the counts are held would put an ODbL record in the feed
the hold keeps it out of. One switch decides both. (DA Leads also strips both
field names from the paid API itself, `_UNLICENSED_SCORE_FIELDS["noise"]`.)

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

The Brisbane row is reference only: Brisbane is `large_aircraft_only`, so the
estimate is not applied there (rule 1). Re-run 2026-11-29 after the rule
change: every figure above is unchanged (`estimate_lden` did not change).

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

## Measured gate (A/B before turning `estimate` on)

`scripts/eis_measured_gate.py --points-out FILE` writes one JSON line per
scored point (`model_lden`, `diff`, `aircraft_db`, `estimate_lden`,
`aircraft_zone`, `aircraft_assessment`, ...). Run it once with
`NOISE_AIRCRAFT_ACTIVITY=0` and once with `estimate`, same corpus, and join
on `key`. The script stubs the result cache (`_cache_get` / `_cache_put`), so
neither run reads or writes `noise_result_cache.sqlite`.
