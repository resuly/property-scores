# Flood Risk Score — Technical Specification

## Status: v1 Functional

| Item | Status |
|------|--------|
| VIC flood overlays (FO/RFO/LSIO/SBO) | Done |
| NSW flood layer (230) | Done |
| SA flood layers (16/17/51) | Done |
| TAS flood layer (3) | Done |
| ACT flood FeatureServer | Done |
| QLD/WA/NT | Not available (returns 85) |
| Product page (flood.html) | TODO |
| Validation vs insurance flood zones | TODO |

## Data Sources

State government ArcGIS planning overlay services:

| State | Endpoint | Layers |
|-------|----------|--------|
| VIC | planning-schemes.api.delwp.vic.gov.au | FO (floodway), RFO (rural floodway), LSIO (land subject to inundation), SBO (special building overlay) |
| NSW | mapprod3.environment.nsw.gov.au | Layer 230 (flood prone land) |
| SA | location.sa.gov.au | Layers 16, 17, 51 (flood hazard) |
| TAS | services.thelist.tas.gov.au | Layer 3 (flood-prone areas) |
| ACT | app.actmapi.act.gov.au | Flood_Planning_Control_Area |

## Scoring Method

```
Base score: 90 (no flood data = low risk assumed)

Severity deductions:
  Floodway (FO):           score = 10-20 (most severe)
  Rural Floodway (RFO):    score = 15-25
  Flood overlay:           score = 20-40
  Land subject to inundation (LSIO): score = 30-50
  Special Building Overlay (SBO):    score = 40-55
  Moderate risk zone:      score = 40-60

Multiple overlapping zones: take the worst (lowest) score.
```

## Output Fields

- score (0-100, 100 = lowest flood risk)
- label (Very Low / Low / Moderate / High / Very High)
- state (detected Australian state)
- flood_zones (list of overlay names that hit)
- zone_count

## Validation Plan

1. **Insurance flood maps** — compare against IAG/Suncorp flood zone ratings (if accessible)
2. **Historical flood events** — check if areas flooded in 2010-2011 VIC floods score as high risk
3. **Melbourne Water flood data** — cross-reference with official 1% AEP flood extents
4. **Brisbane 2022 floods** — test QLD locations (currently no data)

## Known Limitations

1. **No QLD/WA/NT data** — state planning overlays not found via ArcGIS REST. Returns default score 85.
2. **Binary zones** — most overlays are binary (in/out), no depth or probability gradient.
3. **Static data** — planning overlays update infrequently. May miss recent development changes.
4. **No climate projection** — doesn't account for increasing flood risk under climate change.

## Future Improvements

- QLD flood mapping (check QSpatial / FloodCheck)
- WA flood mapping (check SLIP / DWER)
- Flood depth estimation where available
- Climate change projection overlay (2050/2100)
- River proximity + elevation analysis as supplementary factor
