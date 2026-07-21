# reg-09: batch depth-COG build over the open CC-BY candidates (2026-07-21)

Ran `build_depth_cogs.py` over 29 open CC-BY depth-grid candidates. Per Bo's "有几个算几个"
— take what auto-builds, don't grind per-study rules. Full log: `batch_build.log`.

## Outcome: 2 clean councils auto-built + verified in the score

| Council | Depth grid | flood_score check |
|---|---|---|
| Central Coast — Northern Lakes FRMS&P | 1% AEP, 2 m cells | @(-33.28,151.48) 0.76 m → **30 High Risk** |
| Coffs Harbour — Moonee FS 2025 | 1% AEP (TUFLOW `_d_Max`) | @(-30.19,153.15) 3.0 m → **15 Very High Risk** |

Two councils ~350 km apart, both returning real per-cell depth via one code path. Loaded
data-driven from `manifest.json` (STUDY_DEPTH_MANIFEST) — deploying more councils = add COGs
+ manifest rows, no code change.

## Honest yield: 2 of 29 auto-built — the 苦活 is real

- **3 built, 1 dropped:** Wollongong FCT's source tif carried **no CRS**, so the warp left
  it in projected metres mislabelled as 4326 ("Invalid angle" bounds). Caught on validation
  and dropped — not shipped. Fixing = force `-s_srs` per that study.
- **~15 "0 tifs":** candidate was a report/PDF or model-*input* dataset, not depth rasters
  (title-heuristic false positives — the candidate list needs tightening).
- **~7 "N tifs, no 1% depth match":** real grids, but each study names depth differently
  (Coffs Creek 63 tifs ≠ Coffs Moonee's convention; Hawkesbury 27; Tweed 12) → each needs a
  naming rule.
- **3 "no-zip-resource":** SHP-only extent datasets, no depth.

**Takeaway:** a single generic pass yields ~2/29. Reaching the ~19-council ceiling is
per-study curation (naming + CRS + candidate-list cleanup), exactly the moat labour — not a
button. The pipeline + data-driven registry make each added council cheap once its rule is
written; the writing is the work.
