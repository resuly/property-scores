# reg-09 — graded flood hazard into the flood score

> 2026-07-21 · branch `reg-09-flood-depth`. Origin: limon-ops venture reg-09
> (open flood-study extraction). This is the **内部保底价值** path — thicken our own
> flood score, independent of any external licensing.

## What changed vs the approved first-cut design (honest correction)

The first-cut design (`limon-ops verify/reg-09/INTEGRATION-FIRSTCUT.md`) assumed graded
hazard could be added at **near-zero cost via live per-council ArcGIS queries**. Reading
the code overturned that:

- The flood score reads graded flood data from the **local `features.duckdb` layer
  library** (baked by the da_leads tiles pipeline), *deliberately* — live state-service
  queries were removed because the score and the customer-visible hazards block
  contradicted each other (Rocklea case, `local_overlays.py` header). Re-introducing live
  queries would re-open that exact bug.
- The already-baked flood props carry only a coarse category string
  (`"1% AEP / 1 in 100 Year Flood"`), **not** the ARR H1..H6 class. Getting graded hazard
  therefore needs a **re-fetch + re-bake** of those councils with the hazard attribute.

So the work splits cleanly into two halves:

| Half | What | Cost / gate |
|---|---|---|
| **Code (this branch, DONE + tested)** | classify H1..H6 → severity + provenance; surface `flood_hazard` in the score; graded H1 scores safer than H6 | pure code, 15 new + 18 existing unit tests pass, **backward-compatible & dormant until data arrives** |
| **Data (da_leads, GATED)** | add hazard sources to `states.yaml`, re-fetch with the H-class field, re-bake `features.duckdb`, deploy | server-side bake of the ~9.4 GB library → **resource-guarded + deploy approval (Bo)** |

The code half is safe to ship now: with no hazard class in the library it's a no-op; the
moment the re-bake adds the class, the score starts grading. No flag day.

## Code half — what this branch adds

- `flood/local_overlays.py`: `_hazard_class()` normalises the encodings councils publish
  (explicit `H3`, ARR `gridcode` 1..6, coarse low/med/high) → `H1..H6`; `_classify()`
  grades any source named `*_flood_hazard` into severity (H1→moderate … H4-H6→floodway);
  `check()` returns the worst graded hazard with provenance (study/year/licence).
- `flood/score.py`: surfaces `flood_hazard` (`class`, `description`, `aep`, `provenance`,
  `licence`) + a `flood_hazard_summary` one-liner. Uses the graded severity so the
  existing tuned score maths turn an H1 into a milder number than an H6 — the Skirving St
  over-report fix.
- `tests/test_flood_hazard.py`: 15 tests (normalisation, grading, backward-compat, and the
  H1-safer-than-H6 score assertion).

## Data half — per-council bake list (GATED, needs Bo go)

Probed from the 31 already-covered flood services (`limon-ops verify/reg-09/scan/`).
Convention: bake as source `*_flood_hazard`, normalise the class into `props.hazard_class`
(H1..H6), and carry `study`/`year`/`licence` in props for provenance.

| # | Council | State | Layer / field to bake | Class encoding | Ready? |
|---|---|---|---|---|---|
| 1 | Queensland OM (Flood Hazard OpenData) | QLD | `OM_Flood_Hazard…OpenData/0`, field `gridcode`/`OVL2_DESC` | ARR gridcode 1..6 | field confirmed; verify gridcode↔H mapping |
| 2 | Newcastle | NSW | `Newcastle_…_1AEP_Hazard` FeatureServer (H-class attr) | needs layer-id + field confirm | layer id TBD |
| 3 | Central Coast | NSW | `Hazards 100y H1toH6` | H1..H6 named | **image MapServer — may need raster→vector, heavier** |
| 4 | Northern Beaches | NSW | `NaturalRisk/MapServer/1` Flood Hazard Map | field TBD | queryable, confirm class field |
| 5 | Federation (Corowa/Howlong/Mulwala) | NSW | **PDF depth tables (tier C)** — 84 records already extracted | depth m per AEP, no polygons | needs depth polygons to point-query; tabular only for now |

**Honest readiness:** #1 (QLD OM) is the cleanest live-vector hazard to bake first. #2/#4
need one more probe to lock layer id + class field. #3 Central Coast is a raster image
service (heavier — raster reclass, not a simple feature bake). #5 Federation is tabular
(great as external proof + a locality lookup, but not a coordinate polygon join yet).

→ Recommend baking **#1 first as the reference council** end-to-end (states.yaml → re-bake
a local slice → local :8001 before/after on a known over-report point → Bo verify →
deploy), then #2/#4, then decide on #3 (raster) and #5 (tabular) separately.

## Test path before any deploy (per feedback_test_before_show)

1. Build a **local** `features.duckdb` slice with council #1's hazard source (download is
   client-side + small; resource-safe) → point `FEATURES_DB` at it.
2. Run `flood_score` on 4-6 addresses incl. a Skirving St-type binary over-report point;
   show before (binary flood) vs after (graded H-class + milder score where H1/H2).
3. Bo verifies the before/after → only then re-bake the production library + deploy
   (resource guard: no parallel bakes, mem check, monitor — server_resource_guard).
