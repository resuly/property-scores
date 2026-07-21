# reg-09 (b): CC-BY open depth grid → flood score, verified locally

Chain proven end-to-end: **CC-BY open study zip → depth COG → address-level depth in
our flood score**, no account, no production DB.

Source: Central Coast Council "Northern Lakes FRMS&P — Processed Hydraulic Results
(Public)", CC BY 4.0, access_level=open. Built the 1% AEP depth COG with
`build_cc_depth_cog.sh` (GDA94/MGA56 → EPSG:4326, 2 m cells, max depth 3.89 m).

flood_score before/after (3 real floodplain points, known depths):

| Modelled depth | BEFORE (current engine) | AFTER (depth wired in) |
|---|---|---|
| 0.34 m | — | score 45 **Moderate Risk**, flood_depth 0.34 m |
| **1.20 m** | **score 95 "Very Low Risk"** (engine missed it) | **score 15 "Very High Risk"**, flood_depth 1.2 m |
| 2.26 m | — | score 15 **Very High Risk**, flood_depth 2.26 m |
| Sydney CBD (no grid) | 95 Very Low | 95 Very Low (unchanged — backward compat) |

The 1.2 m case is the headline: the coarse state overlay + satellite missed it entirely
(Very Low Risk); the council depth grid puts 1.2 m of water there → now Very High Risk,
with `flood_depth_summary = "~1.2 m modelled depth at 1% AEP (Central Coast Council …,
CC BY 4.0)"`. Depth is the most authoritative signal and sets the score; a positive depth
can no longer read Very Low.

Tests: `tests/test_flood_study_depth.py` (7) + hazard (15) + existing flood (18) = 43 pass.
Depth is COG-independent in tests (monkeypatched) and dormant in prod until a depth COG is
deployed — same safe-code / gated-data split as the graded hazard.

## Gated production step (needs Bo go + resource guard)
Deploy per-council 1% AEP depth COGs to a store (`/data/flood/study_depth/…`) and register
their bbox in `study_depth._REGISTRY`. Start with this one CC-BY council; scale across the
~29+ confirmed open CC-BY grids. Building each COG is client-side + light (this one: 9 MB).
