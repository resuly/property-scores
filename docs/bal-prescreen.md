# Indicative BAL Pre-screen (AS 3959 Method 1)

Status: **verification prototype** (reg-08, 2026-07-20). Not wired into the API or
the customer surface. Built to answer one question, *can we produce an indicative
Bushfire Attack Level from a coordinate, cheaply, on open data?*, and to serve as
the proof artifact for the venture registry.

## What it does

`property_scores.bal_prescreen.bal_prescreen(lat, lng)` returns an **indicative**
BAL (`BAL-LOW` / `BAL-12.5` / `BAL-19` / `BAL-29` / `BAL-40` / `BAL-FZ`) plus a
confidence band and full input transparency, the three AS 3959 Method 1 inputs a
free calculator makes a designer hand-enter, automated from the coordinate:

1. **FDI region**, AS 3959 Table 2.1 by state (+ elevation-based alpine override).
2. **Vegetation class + distance**, nearest classified vegetation patch (>=1 ha,
   within 100 m) from ESA WorldCover 10m, geodesic distance to the site.
3. **Effective slope + direction**, local DEM / 5 m LiDAR slope, with up/down
   direction inferred from site-vs-vegetation elevation.

It then looks up the indicative BAL in the AS 3959 Method 1 distance tables, takes
the worst across vegetation types, and cross-checks against the official state
planning overlay (BMO / BPL) as an independent signal.

## Why it is only a pre-screen (the honest limits)

This is **NOT** a certified BAL and must never be used for a building permit. A
compliant BAL requires an accredited assessor measuring distance and slope and
classifying vegetation on the ground, per quadrant. Specific limits:

- **Vegetation formation is the dominant uncertainty.** ESA WorldCover 10m gives a
  land-cover class (tree / shrub / grass) but *not* canopy-cover %, so it cannot
  separate Forest (A, heaviest) from Woodland (B) or Rainforest (F). We take the
  conservative (heaviest) class as the point estimate and expose the lighter
  plausible class as the low end of the confidence band. This is the known 硬伤
  recorded in the registry: "植被分类误差会跨 BAL 档，必须带置信带."
- **>=1 ha contiguity is screened with connected components** in the local 10 m
  WorldCover window. It is still a raster approximation rather than a surveyed
  vegetation boundary, so fine or partly clipped patches can be misclassified.
- **Slope direction is inferred**, not surveyed, from the elevation difference
  between the site and the nearest vegetation pixel.
- **Distances are modelled** from 10 m rasters, not measured. Sub-10 m setbacks
  (which decide FZ vs BAL-40) are below the input resolution.
- **A coordinate that lands *in* the bush reads BAL-FZ** (distance 0). A real house
  lot is set back from the vegetation, so production use must run the lot's building
  envelope / address point, not a locality centroid (see demo caveat below).

## Method sources (all public)

- **BAL computation data (FDI 100/80/50/40):** pinned to Geoscience Australia's
  Apache-2.0 BAL Toolbox `utilities/bal_database.py` at commit
  `18c6cff4b37544805e78cf00ec376dbca2ff8cd0`. The product remains a preliminary
  screen based on the 2009 Method 1 model, not a current AS 3959 conformity claim.
- **FDI by jurisdiction (Table 2.1):** Geoscience Australia **BAL Toolbox** docs
  (open-source, GA 2017), `background.html` / `bal.html`. The toolbox is GA's own
  open implementation of AS 3959 Method 1 and is the registry's cited method
  背书.
- **AS 3959-2018** revised construction provisions but the Method 1 distance tables
  are materially unchanged; using the 2009 tables for an *indicative* pre-screen is
  honest and is flagged in every result (`method` field).

The pinned GA implementation supplies all four Australian FDI branches, so SA/WA
now use FDI 80 and QLD/NT use FDI 40 directly. Unknown FDI values still fail into
an explicitly marked substitution and lower confidence.

## Reuse (why this was cheap to build)

Almost every input already exists in the bushfire module:
`bushfire.landcover_grid` (WorldCover), `bushfire._terrain_slope` (DEM/LiDAR),
`bushfire._overlay_check` (official overlays), `common.terrain.elevation`,
`common.au_state.detect_state`. This module adds only the AS 3959 Method 1 layer:
nearest-vegetation distance, effective-slope direction, the lookup tables, and the
confidence band.

## Demo (reg-08 verification, VIC + NSW)

`scripts/bal_prescreen_demo.py` runs 12 curated coordinates and writes
`bal_prescreen_demo_output.json`. Result summary (2026-07-20):

| Site | Context | Indicative BAL | Range | Conf | Veg dist | Overlay |
|------|---------|----------------|-------|------|----------|---------|
| Melbourne CBD | dense urban | BAL-LOW | LOW..LOW | high | none<100m | outside |
| Geelong CBD | regional urban | BAL-LOW | LOW..LOW | high | none<100m | outside |
| Glen Waverley | SE Melb suburb | BAL-LOW | LOW..LOW | high | none<100m | outside |
| Lorne | steep Otways forest | **BAL-29** | 19..40 | moderate | 28.9 m | in_zone |
| Halls Gap | Grampians forest | BAL-FZ | 40..FZ | moderate | 12.0 m | in_zone |
| Blackheath (NSW) | Blue Mtns forest | BAL-FZ | 29..FZ | **low** | 18.1 m | outside* |
| Warrandyte / Kalorama / Kinglake / Mt Dandenong | in forest (centroid) | BAL-FZ | FZ..FZ | moderate | 0 m | in_zone |
| Lismore VIC plains | grassland | BAL-LOW | LOW..LOW | moderate | 0 m (grass) | outside |
| Anglesea edge | coastal, grass nearest | BAL-LOW | LOW..LOW | moderate | 89.6 m (grass) | in_zone |

What the demo proves and honestly shows:

- **End-to-end automation works**, coordinate in, indicative BAL + transparent
  inputs out, no hand-entry.
- **Results track reality and the official overlay**, urban = LOW/outside,
  forest = high/in_zone.
- **The graded middle works** (Lorne BAL-29, range 19..40 at ~29 m from forest).
- **Confidence banding + overlay cross-check earn their keep**, Blackheath is
  flagged *low* confidence because WorldCover fuel is close but the official overlay
  reads "outside" (`*` disagreement surfaced, not hidden).
- **Grassland follows the GA implementation:** for FDI other than 50, grassland
  inside 50 m is assessed and grassland from 50 m is excluded. The earlier code
  incorrectly excluded all non-FDI-50 grassland.
- **Centroid caveat**, several localities read BAL-FZ because the centroid sits
  literally in bush (distance 0). Production must screen the address/lot point, not a
  suburb centroid; Lorne is the representative real-lot-style result.
