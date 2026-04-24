# Flood Risk Score — Technical Specification

## Status: v2 (Overlay + JRC Satellite)

| Item | Status |
|------|--------|
| VIC flood overlays (FO/RFO/LSIO/SBO) | Done |
| NSW flood layer (230) | Done |
| SA flood layers (16/17/51) | Done |
| TAS flood layer (3) | Done |
| ACT flood FeatureServer | Done |
| JRC Global Surface Water (all AU) | Done (v2) |
| QLD/WA/NT coverage via JRC | Done (v2) |
| Product page (flood.html) | TODO |

## Architecture

Two complementary signals combined into a single score:

1. **ArcGIS Planning Overlays** — official government flood zones (VIC/NSW/SA/TAS/ACT)
2. **JRC Global Surface Water** — 38 years of Landsat satellite data (global, 30m resolution)

The final score uses `min(overlay_score, jrc_score)` — the worse signal wins.

## JRC Global Surface Water (New in v2)

- **Source**: European Commission Joint Research Centre, 1984-2021
- **Resolution**: 30m (Landsat-derived)
- **Access**: Cloud Optimized GeoTIFF via Microsoft Planetary Computer
- **Latency**: ~2s cold start, ~15ms subsequent reads (COG caching)

### How It Works

1. Sample a 1.1km x 1.1km grid (11x11 = 121 points) around the property
2. Read water occurrence values (0-100% of time water observed)
3. Classify cells:
   - **Permanent water** (>90%): rivers, lakes, bays — not flood risk per se
   - **Flood cells** (1-90%): areas that sometimes flood — the real risk signal
4. Score based on: number of flood cells, distance, ratio to permanent water

### JRC Scoring Logic

| Condition | Score |
|-----------|-------|
| No wet cells within 500m | 95 |
| Only permanent water nearby | 70-85 |
| 10+ flood cells | 15-30 |
| 5+ flood cells, high flood ratio | 25-40 |
| 5+ flood cells, mixed | 55 |
| 1-4 flood cells, close | 55-75 |

Key insight: a few flood cells adjacent to a permanent river = normal water-level fluctuation (mild risk). Many flood cells or flood cells far from rivers = actual flood plain (high risk).

## State Overlay Data Sources

| State | Endpoint | Layers |
|-------|----------|--------|
| VIC | plan-gis.mapshare.vic.gov.au | FO (floodway), RFO (rural), LSIO (1% AEP), SBO |
| NSW | mapprod3.environment.nsw.gov.au | Layer 230 (flood prone) |
| SA | location.sa.gov.au | Layers 16, 17, 51 (flood hazard) |
| TAS | services.thelist.tas.gov.au | Layer 3 (filtered by O_NAME) |
| ACT | services1.arcgis.com | ACTGOV_FLOOD_EXTENT |

## Validation (2026-04-24)

| Location | Score | Label | Notes |
|----------|-------|-------|-------|
| Maribyrnong (VIC flood 2022) | 40 | Moderate | LSIO overlay + JRC 4 flood cells at 142m |
| Melbourne CBD | 55 | Moderate | JRC Yarra River edge |
| St Kilda | 75 | Low | Near bay (permanent), not flood-prone |
| Brisbane River (QLD) | 75 | Low | NEW: JRC coverage, permanent river |
| Lismore (NSW flood 2022) | 40 | Moderate | JRC 5 flood cells at 348m |
| Darwin (NT) | 30 | High | NEW: JRC 17 flood cells, monsoon zone |
| Perth (WA) | 95 | Very Low | NEW: JRC confirms no flood evidence |
| Mt Dandenong | 90 | Very Low | High ground, no flood evidence |

## Output Fields

- score (0-100, 100 = lowest flood risk)
- label (Very Low / Low / Moderate / High / Very High)
- state (detected Australian state)
- flood_zones (ArcGIS overlay names)
- zone_count
- jrc (satellite data: max_occurrence_pct, flood_cells, wet_cells, nearest_water_m)

## Known Limitations

1. **JRC resolution (30m)** — narrow streams may be missed
2. **JRC is backward-looking** — shows where water HAS BEEN, not future flood risk
3. **No depth estimation** — binary flood/no-flood per cell
4. **Remote COG latency** — first query ~2s (cold start), subsequent ~15ms
5. **Overlay gaps** — QLD/WA/NT still lack official planning overlays

## Future: HAND + ERA5

- **HAND (Height Above Nearest Drainage)**: 30m pre-computed dataset on AWS S3 (~5GB for AU). Physical vulnerability measure — how high above the nearest waterway.
- **ERA5 P95 Precipitation**: Pre-computed extreme rainfall grid. Compound risk indicator.
