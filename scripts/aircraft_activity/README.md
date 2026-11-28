# Aircraft activity pipeline (ADS-B → per-address overflight grids)

Builds, for 14 Australian city regions, gridded counts of low aircraft overflights from the public adsb.lol
trace archive, plus airport movement checks against Airservices and helicopter landing sites. Heavy work runs
on a Google Colab VM; a Mac drives it with the `colab` CLI and relays the results into Google Drive.
Only source code lives here: no trace data, no grids, no credentials.

## Data sources and licences

| Source | Use | Licence / terms |
|---|---|---|
| adsb.lol `globe_history_<year>` GitHub releases (readsb `trace_full` JSON, one archive per UTC day) | all trajectories | ODbL 1.0: keep attribution "adsb.lol"; derived databases that are made public must stay under ODbL |
| OurAirports `airports.csv`, `runways.csv` | airport and heliport positions, runway ends, names | public domain |
| OpenStreetMap via the Overpass API | tag clustered landing sites as `hospital` (hospital or hospital helipad within 200 m); `name_helipads.py`: helipad names and hospital polygons around a few sites | ODbL 1.0, "© OpenStreetMap contributors" |
| OpenStreetMap via our basemap tiles (`tiles.limontech.net`, OpenMapTiles schema) | `name_helipads.py`: hospital / emergency / hotel POIs, aerodrome labels, mapped helipads near landing sites | ODbL 1.0, "© OpenStreetMap contributors" |
| SRTM 1" DEM (AWS terrain tiles, `elevation-tiles-prod/skadi`) | ground elevation for height above ground | public domain (NASA/USGS) |
| Airservices Australia "Movements at Australian Airports" monthly PDFs | validation only (visible-movement ratios); never redistributed | Airservices website terms |
| VicPlan MAEO overlay (only in the earlier Melbourne comparison) | validation only | Victorian Government data terms |

## Definitions (same in the 14-day and 12-month runs)

- Region boxes: 60 km half-size around each main airport (Melbourne: 37.25–38.45 S, 144.25–145.75 E). Grid boxes
  are 1.5 km inside the data box (Melbourne grid 37.40–38.30 S, 144.40–145.60 E).
- Grid: 250 m cells (`dlat` = 250/111132 deg, `dlon` = 250/(111320·cos(mid-lat)) deg); cell centre = (lat0 + i·dlat, lon0 + j·dlon), i north, j east.
- Event: an aircraft passing within **1,000 m horizontally** of the cell centre while **≤ 3,000 ft above the ground at
  the cell centre**. Positions between consecutive trace points ≤ 120 s apart are linearly interpolated every ≤ 100 m.
  The same aircraft counts once per cell per **10 minutes**; the event's height is the height at its closest sample.
- Height: geometric (GNSS) altitude when present, else barometric altitude + the hour's median (geometric − barometric)
  offset from ADS-B aircraft in the region; ground elevation from SRTM.
- Night = local 22:00–06:00. Weekday/weekend by local date (public holidays are not treated specially).
- Classes: helicopter = emitter category A7 or a helicopter ICAO type designator; jet = category A3–A5; light = everything
  else that flies (A1, A2, unknown, B-categories). Surface vehicles (C*) are dropped. A missing or A0 category is
  filled with the most common category of the same ICAO type seen in the data.
- Airport movements: low passes inside a zone around the runways (radius = farthest runway end + 1.5 km, clamped
  2–4.5 km) below 500 ft AGL; an arrival or departure counts only when the episode reaches ≤ 250 ft (or ≤ 100 ft for the
  strict variant) or reports ground. Touch-and-go = 2 movements, matching Airservices' "(arrivals + circuits) × 2".
- Runway-end usage: each counted arrival/departure is assigned to the runway end whose direction of travel is within
  35° of the aircraft's ground track below 500 ft (arrival: from entering the zone to the first ground report; departure:
  from the last ground report to leaving), using OurAirports runway-end coordinates; otherwise `unk`.
- Approach low-coverage: share of fixed-wing descents inside 3 km of a runway end below 1,000 ft that are seen at
  ≤ 200 ft or on the ground.
- Helicopter landing sites: helicopter stops below 100 ft AGL or on the ground, preceded/followed by a point ≥ 100 ft,
  clustered at 150 m; stops of one aircraft at one cluster less than 10 minutes apart are merged. Published sites need
  ≥ 2 movements, are > 2 km from any aerodrome, are not over the sea without a ground report, and have a ground report
  or ≥ 2 arrivals.
- Local days: a local date is used only when both UTC archives that cover it are complete; a UTC day with no
  release, a failed download/processing, or an archive under half the median size removes its two local dates
  (listed in `missing_days`).

## Files

| File | Where it runs | What it does |
|---|---|---|
| `make_day_plan.py` | Mac | picks one archive per UTC day (prod-0 unless staging-0 is >10% larger) → `day_plan.csv` |
| `nat_common.py` | VM | shared helpers: regions, OurAirports, DEM, airport episode detection |
| `year_pipeline.py` | VM | `fetch` (download + untar, ≤ 2 days ahead) and `process` (parse once, all 14 regions in parallel) per UTC day |
| `year_rollup.py` | VM | rolls UTC-day aggregates into local-month aggregates once all UTC days of the month are done |
| `year_assemble.py` | VM | monthly → annual grids, quarterly grids, monthly airport/approach/runway-end tables, helicopter site clusters |
| `year_summary.py` | Mac | 12-month summary CSVs (monthly movements vs Airservices, approach coverage, test points, runway probes, MAEO IoU, helipads) |
| `year_pack.py` | VM | background loop: packages finished work into small tarballs and writes `PROGRESS.md` |
| `year_relay.sh` | Mac | low-frequency poll: downloads new tarballs into the local Google Drive folder |
| `year_finalize.py` | Mac | annual `grid_/meta_/helipads_<city>` + quarterly grids for the prototype |
| `name_helipads.py` | Mac | names unnamed landing sites from OSM (helipad tags, else the hospital / facility it sits at, only with a mapped helipad or a busy site; ambiguous -> no name); writes to a separate folder; rules in its docstring |
| `colab_launch.sh` | Mac | starts or resumes the VM jobs |
| `parse_airservices.py` | Mac | Airservices monthly PDFs (after `pdftotext -layout`) → CSV |
| `extract_multi.py`, `nat_run.py` | VM | the 14-day run (2026-11-13..26), kept for reproducibility |
| `finalize_14day.py`, `summary_14day.py`, `heli_table_14day.py`, `selfcheck_testpoints.py` | Mac | 14-day outputs and checks |

## Running from scratch

```bash
# Mac
pip install google-colab-cli   # `colab` CLI, authenticated (colab whoami)
python make_day_plan.py 2025.11.26 2026.11.26 "$AA_DRIVE_DIR/day_plan.csv"
bash colab_launch.sh start yr 2025.11.26 2026.11.26     # CPU High-RAM VM; returns immediately
nohup bash year_relay.sh yr 600 > relay.log 2>&1 &      # every 10 min: pull finished packages into Drive
```

The VM keeps raw archives only in `/content/yraw` and deletes each day after processing. Throughput measured on a
Colab CPU High-RAM VM (8 cores, 50 GB): about 55–80 s per UTC day for all 14 regions with downloads overlapped, so a
full year is roughly 7–9 hours.

### Parallel run (several VMs)

One VM is CPU-bound (parsing ~45 s and aggregation ~25-30 s per day on 8 cores; downloads overlap), so speed-up comes
from more VMs, each taking a month-aligned block of UTC days (a month's rollup needs all its UTC days on one VM, plus
the last day of the previous month). Only the main VM assembles grids (`AA_ASSEMBLE=1`); the others get a shard tag so
their package names do not collide, and all start from the same `typecat.json` (otherwise the jet/light split of a
day can differ by up to ~0.4% depending on which VM ran it; totals and helicopter counts do not depend on it).

```bash
AA_TAG=b AA_ASSEMBLE=0 AA_TYPECAT=typecat_seed.json bash colab_launch.sh start yr2 2026.04.30 2026.07.31
AA_TAG=d AA_ASSEMBLE=0 AA_TYPECAT=typecat_seed.json bash colab_launch.sh start yr4 2026.07.31 2026.11.26
nohup bash year_relay.sh "yr yr2 yr4" 600 > relay.log 2>&1 &     # one low-frequency loop for all VMs
```

Measured on the 2025-11-27..2026-11-26 run: 3 VMs (Colab refused a 4th session), shards yr = UTC 2025.11.26..2026.04.30
plus 2026.06.30..07.31, yr2 = 2026.04.30..06.30, yr4 = 2026.07.31..11.26; processing took ~3.2 h against ~7.5-8 h on one
VM (speed-up ~2.3x; the non-US VMs ran 80-120 s/day against 55-80 s/day), and the whole run used 3.4 compute units.
A failed untar is retried and then taken from the other archive instance (staging-0/prod-0); failed days are retried
when the processor restarts, and a month is rolled up again once a dropped day succeeds.

Check `colab usage` first (CPU High-RAM ~0.26 compute units per VM-hour). On the account used in 2026-11, Colab
allowed 3 concurrent sessions. When the shards finish, `year_merge.sh` uploads their `month/<YYYY-MM>` folders (from Drive `monthly/month/`;
set `AA_MERGE_MONTHS` to the shards' months) and the `*_rwy.csv` daily files to the main VM and assembles all regions
there. Keep each upload under ~100 MB (the Colab contents API returned HTTP 500 for a 376 MB file) and copy many small
files out of the Drive folder in parallel (`xargs -P 16 cp`): reading them one by one through the Drive client is slow. Downloads use `aria2c` (16 connections) when it is installed; on a VM far from GitHub's CDN a single curl
stream was about ten times slower.

When the run ends (`/content/aa_out/FINISHED` on the main VM), on the Mac:

```bash
python year_finalize.py MEL --raw "$AA_DRIVE_DIR/grids-12m/raw/out" --official airservices_rows_monthly.csv \
    --dest "$AA_DRIVE_DIR/grids-12m" --dest /path/to/prototype/aircraft-activity-12m --phase2-meta /path/to/14-day/meta
colab stop -s yr
```

## Google Drive relay and resuming

The VM never mounts Drive (that needs a person to approve each new VM) and no Google token is copied to it.
`year_pack.py` writes everything worth keeping to `/content/aa_out/`:

- `pkgs/daily_<first>_<last>.tar`: per-UTC-day aggregates for 5 days (no registrations; aircraft are reduced to a salted
  hash only inside helicopter stop rows). About 10 MB per day for all 14 regions, mostly the hourly grid counts.
- `pkgs/month_<YYYY-MM>.tar`: the local-month rollup.
- `pkgs/grids_<regions>_<months>.tar`: (partial-)year grids, quarterly grids and tables, refreshed after each month
  (Melbourne and Sydney first).
- `PROGRESS.md`, `done.jsonl`.

`year_relay.sh` unpacks them into `<Drive>/_limon-ops/aircraft-activity/{daily,monthly,grids-12m,results-year}`
and copies `PROGRESS.md`; the Drive desktop client uploads them. Once a month is rolled up, its daily files are
dropped from Drive, so Drive holds the monthly rollups plus at most about one month of daily files. Every `colab`
call uses `</dev/null` and returns at once; no long exec stays attached.

If the VM is reclaimed, start a new one with `bash colab_launch.sh resume <new-session> 2025.11.26 2026.11.26`: it
uploads the rolled months and the remaining daily files from Drive, marks those days done, and continues. At most
the days since the last relayed package are recomputed.

## Output format (prototype files)

- `grid_<city>.npz`: scalars `lat0, lon0, dlat, dlon, ny, nx`; float32 `[ny, nx]` arrays `passes_per_day`,
  `night_per_night`, `weekday_per_day`, `weekend_per_day`, `median_agl_ft` (NaN where no events; from 100 ft bins in the
  12-month run), `heli_per_day`, `heli_night_per_night`, `jet_per_day`, `light_per_day`; `hourly` `[ny, nx, 24]`
  (sums to `passes_per_day`).
- `grid_<city>_q1..q4.npz` (12-month only): `passes_per_day`, `night_per_night`, `jet_per_day`, `heli_per_day` per
  calendar quarter of the local date.
- `meta_<city>.json`: dates, day counts, `missing_days`, timezone, source, coverage label and note, per-airport
  visible ratios against Airservices (low = 100 ft, high = 250 ft threshold).
- `helipads_<city>.json` after `name_helipads.py`: also `name_source`, `name_kind`, `name_dist_m` (and `name_candidates` when ambiguous).
- `helipads_<city>.json`: name (OurAirports name, else "Unidentified landing site"), kind, source, lat, lon, arrivals,
  departures, night_movements, aircraft_count, hourly[24]. No registrations or owners.
