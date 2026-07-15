# Handover: noise cold-path optimisation — the transfer RF (`transfer_lden`)

**2026-07-15 16:51 AEST** · scoped follow-up from the score-perf deep-dive.

## TL;DR

`noise_score` cold latency is **1–3 s in dense urban areas**. Profiling on prod
pinned the dominant cost to **`transfer_lden` (the opt-in EU→AU transfer Random
Forest)** — **1.7–2.2 s in dense areas, ~190 ms in sparse ones**. Everything else
is already handled: the aircraft external calls are parallelised (fully hidden),
and a shared persistent SQLite cache makes every *repeat* visit sub-ms. This doc
is for a fresh session to profile *inside* `transfer_lden` and cut that 2 s
**without changing the RF's predictions/scores**.

The transfer RF is **not a bug** — it's a quality feature Bo turned on
(`NOISE_TRANSFER=1` on prod) that replaces the physics Lden with a
geometry-trained prediction. So this is an optimisation / speed-quality call, not
a fix.

---

## What's already done and deployed (don't redo)

All on `property-scores` `master`, deployed to prod `:8099`:

1. **Aircraft call parallelised** (`noise/score.py`): `aircraft_noise_penalty`
   is two external API round-trips (~half the *old* cold time). It's now fired in
   a thread at the top of `noise_score` and joined at its use site, so it overlaps
   the DuckDB work. Measured `aircraft_wait = 0 ms` on prod — fully hidden.
2. **Shared persistent SQLite result cache** (`common/result_cache.py`, used by
   `noise_score`): WAL-mode SQLite file `data/noise_result_cache.sqlite`, shared
   across the 2 uvicorn workers and surviving restarts, self-populating. A
   revisited point drops from ~1 s to ~0.04 s. Degrades to no-cache on any error.
   → **Consequence for this task**: each point pays the transfer cost *once*, then
   serves from cache forever. So the optimisation only helps *first-ever* visits
   to dense areas (map exploration). Weigh the effort accordingly.
3. Micro-opt in `transfer_feats` (compute the road `ST_Distance` once via a
   subquery) — **turned out to be a no-op** (DuckDB already CSE's it). The cost is
   elsewhere; see below.

---

## Where the time goes (measured on prod)

I temporarily added a `_perf_ms` breakdown to the noise payload (since removed —
commit `93a39dc`) and hit fresh dense inner-east Melbourne points with
`?nocache=1`. Representative dense point (total 3170 ms):

| phase | ms | what |
|---|---|---|
| `db_and_sources` | 884 | buildings + AADT/NFDH/Overture roads + rail DuckDB queries & CRTN loops |
| `aircraft_wait` | **0** | already parallelised ✓ |
| `physics_terrain` | 0 | terrain attenuation (negligible) |
| **`transfer_rf`** | **2230** | **`transfer_lden` — THE target** |
| `ml_and_rest` | 55 | ML residual is disabled by default |

Sparse points: `transfer_rf` ≈ 190 ms. **So the cost scales hard with density**
(more roads / buildings / POIs in the bbox), which points at the feature
extraction, not the RF `.predict` (single row, 75 features → microseconds).

I did **not** yet profile *inside* `transfer_feats` — that's step 1 below.

---

## Anatomy of the bottleneck: `noise/transfer.py`

`transfer_lden(db, lat, lng, state)` →
1. `_load()` — lazy-loads the 190 MB RF pickle + calibration JSON once per process
   (`RF_PATH = data/noise_transfer_rf.pkl`, `CALIB_PATH =
   data/noise_state_calibration.json`). Not per-call.
2. **`transfer_feats(db, lat, lng)`** — builds 75 features. This is where the time
   almost certainly goes. It runs, in sequence:
   - **Roads query**: `read_parquet(overture_roads.parquet)` (1.1 GB), bbox
     ±0.013° (~1.4 km), `ST_Distance(geometry, point)` per segment. Dense areas =
     thousands of linestring segments → lots of geometry math + column scan.
   - **Buildings query**: `read_parquet(overture_buildings.parquet)` (2 GB), bbox
     ±0.004/0.003°, centroid X/Y. Dense = many rows.
   - **POIs query**: `read_parquet(overture_pois.parquet)` (259 MB), bbox
     ±0.006/0.0045°.
   - **DEM raster**: `rs.sample(DEM)` + `rs.window_stats(DEM, 300)`
     (`DEM = data/global/dem.vrt`). Fixed window — shouldn't scale with density.
   - **Land-cover raster**: `rs.window_stats(LC, 300, categorical)` +
     `rs.window_stats(LC, 100)` (`LC = data/global/lc.vrt`). Fixed window.
3. `_RF.predict(X)` + per-state affine + `quiet_relief`. Fast.

**Leading hypothesis**: the density-scaling + prod-slow cost is the DuckDB
parquet scans (roads geometry column, buildings) — I/O-bound on Oracle's disk for
dense areas (many rows to read from big parquets). On the Mac the parquets are in
OS file cache/RAM, so local cold is ~5× faster and *hides this*. Confirm by
profiling on prod, or by timing the individual queries.

---

## How to test locally

The whole service runs on the Mac; the data (parquets, rasters, RF pickle) is in
`property-scores/data/`.

```bash
cd ~/Documents/GitHub/property-scores

# 1) The transfer RF is OPT-IN and NOT in .env — you MUST enable it or you won't
#    reproduce the bottleneck at all (it falls back to physics = fast):
export NOISE_TRANSFER=1

# 2) Force a genuine cold recompute (skip both caches):
#    - the pre-baked regional cache: pass ?nocache=1 to the API, OR call the fn directly
#    - the dynamic sqlite cache: delete it, or use fresh coords each run
rm -f data/noise_result_cache.sqlite*

# 3a) Direct function call + cProfile (cleanest for finding the hotspot):
NOISE_TRANSFER=1 .venv/bin/python -c "
import cProfile, pstats, io
from property_scores.noise.score import noise_score
noise_score(-37.70, 145.00)                      # warm imports/data
pr=cProfile.Profile(); pr.enable()
noise_score(-37.8225, 145.0435)                  # a DENSE inner-east point
pr.disable()
s=io.StringIO(); pstats.Stats(pr,stream=s).sort_stats('cumulative').print_stats(25)
print(s.getvalue())
"

# 3b) Or via the API (matches prod path incl. the prebaked lookup):
NOISE_TRANSFER=1 .venv/bin/python -m uvicorn property_scores.api.main:app --port 8099 &
curl -s "http://127.0.0.1:8099/scores/noise?lat=-37.8225&lng=145.0435&nocache=1" | python3 -m json.tool
```

**Important local caveat**: the Mac has the parquets in RAM/OS-cache, so if the
bottleneck is disk I/O it will look *fast locally and slow on prod*. To profile
the real prod cost, either (a) re-add the `_perf_ms`-style timing inside
`transfer_feats` (around each query + each raster call), deploy transiently, hit
fresh dense points with `?nocache=1`, read the breakdown, then revert — the exact
loop I used at the `transfer_lden` level; or (b) SSH to the box and cProfile there
(`/var/www/property-scores`, `.venv`).

### Profiling on prod (how I did it)

Add timing markers, expose them in the payload, deploy, measure, revert:
- Markers were `time.perf_counter()` at phase boundaries in `noise_score`, summed
  into `result["_perf_ms"] = {...}` right before the cache-put. Do the same inside
  `transfer_feats` (roads_q, bldg_q, poi_q, dem, lc, predict).
- Deploy = `git pull && sudo systemctl restart property-scores.service` on the box.
- Hit **fresh** coords (never-cached) with `?nocache=1` so you get a real cold run.
- **Revert the instrumentation before finishing** (don't leave `_perf_ms` in prod).

---

## Directions to investigate (roughly in order)

1. **Profile inside `transfer_feats`** first — confirm whether it's the roads
   query, buildings query, POIs query, or the raster window_stats. Don't optimise
   blind (I already burned a change on the roads `ST_Distance`, which DuckDB had
   already optimised).
2. **Parallelise the independent feature queries.** `get_db()` returns a per-call
   cursor off one shared connection, and its docstring says *separate cursors are
   the supported concurrent-read pattern* (all queries are read-only). So the
   roads / buildings / POIs / DEM / LC fetches can run in a `ThreadPoolExecutor`,
   each with its own `get_db()` cursor — overlapping their I/O. Same pattern
   `heat_island_score` uses. This is the most promising low-risk win if the cost
   is I/O-bound.
3. **Tighten the roads bbox.** It's ±0.013° (~1.4 km) but features only use
   distances ≤ 1000 m and rings ≤ 500 m. A tighter bbox scans fewer segments —
   but **it must not drop any road that contributes to a feature** (check the
   `_invd` term, which uses all distances, and `nearest_major`). Validate scores
   are identical.
4. **Expand the pre-baked regional cache** (`noise/cache.py`,
   `precompute_noise.py`, `noise_cache_*.parquet`) to cover dense metros, so those
   points never hit the live transfer path. This is the "data-baking" route — no
   algorithm change, guaranteed same scores, but a batch job to run/maintain.
5. **Raster sampling** — if `rs.window_stats(DEM/LC, ...)` is a chunk of the time,
   check `common/landcover.py` sampler for per-call raster re-opens vs a cached
   handle, and whether the window read can be smaller.
6. Only if all else fails: reconsider whether the transfer RF earns its cost
   given the cache already amortises it (a product call for Bo, not an eng one).

## Hard constraint: don't change the scores

`transfer_feats` feeds the RF. **Any query/optimisation change must produce
byte-identical features → identical `raw`/`lden` → identical score.** Validate by
scoring a fixed set of ~20 points (mix of dense/sparse/rural, incl. the anchors in
`scripts/anchor_sweep.py` land) before and after, and diffing `score` +
`estimated_db` + `lden_db`. The `NOISE_MODEL_VERSION` string in `noise/score.py`
gates the pre-baked cache — bump it only if you intentionally change outputs.

## Key files & data

- `property_scores/noise/transfer.py` — `transfer_lden`, `transfer_feats`, `_load`.
- `property_scores/noise/score.py` — `noise_score` (calls transfer at ~L810), the
  sqlite cache wiring, the aircraft parallelisation.
- `property_scores/common/result_cache.py` — the shared cache helper.
- `property_scores/common/overture.py` — `get_db()` (per-cursor, concurrent-read).
- Data: `data/overture_roads.parquet` (1.1 G), `overture_buildings.parquet` (2 G),
  `overture_pois.parquet` (259 M), `global/dem.vrt`, `global/lc.vrt`,
  `noise_transfer_rf.pkl` (190 M), `noise_state_calibration.json`.

## Deploy / ops notes

- prod service: `property-scores.service` (systemd, `--workers 2`, `127.0.0.1:8099`),
  code at `/var/www/property-scores` on `master`.
- Deploy: `git pull origin master && sudo systemctl restart property-scores.service`
  (restart clears the in-memory state; the sqlite cache persists on disk).
- ⚠️ **git gotcha**: a prior `sudo git` left root-owned `.git/objects`, so `ubuntu`
  pulls fail with *"insufficient permission for adding an object / failed to write
  object"* (NOT disk-full). Fix: `sudo chown -R ubuntu:ubuntu .git`.
