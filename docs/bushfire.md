# Bushfire Risk Score — Technical Specification

## Status: v1 Functional

| Item | Status |
|------|--------|
| VIC BMO overlay (layer 19) | Done |
| NSW BPL + category (layer 229) | Done |
| WA OBRM-023 (layer 8) | Done |
| SA 6 bands (layers 9-14) | Done |
| TAS (layer 3) | Done |
| QLD | Not available |
| Product page (bushfire.html) | TODO |
| Validation vs CFA data | TODO |

## Data Sources

| State | Endpoint | Layers | Categories |
|-------|----------|--------|------------|
| VIC | planning-schemes.api.delwp.vic.gov.au | BMO (layer 19) | Binary overlay |
| NSW | mapprod3.environment.nsw.gov.au | BPL (layer 229) | Veg Cat 1 (extreme), Cat 2 (high), Cat 3 (moderate), Buffer (low) |
| WA | services.slip.wa.gov.au | OBRM-023 (layer 8) | Binary overlay |
| SA | location.sa.gov.au | Layers 9-14 | 6 hazard bands (high→low) |
| TAS | services.thelist.tas.gov.au | Layer 3 | Filtered by bushfire keyword |

## Scoring Method

```
SEVERITY_SCORES = {
    "extreme":  (5, 15),    # BAL-FZ/BAL-40 equivalent
    "high":     (15, 30),   # BAL-29/BAL-19
    "moderate": (30, 50),   # BAL-12.5
    "low":      (50, 65),   # Buffer/edge zones
}

NSW d_Category mapping:
  "Vegetation Category 1" → extreme
  "Vegetation Category 2" → high
  "Vegetation Category 3" → moderate
  "Vegetation Buffer"      → low

No overlay hit: score = 90 (assumed low risk)
Multiple zones: take the worst
```

## Output Fields

- score (0-100, 100 = lowest bushfire risk)
- label
- category (severity classification if available)
- bushfire_zones (list of overlay names)

## Validation Plan

1. **CFA Bushfire History** — check 2009 Black Saturday affected areas score as extreme
2. **BAL assessments** — compare with known BAL ratings for specific properties
3. **Insurance bushfire zones** — cross-reference with insurer risk zones
4. **Blue Mountains 2019-20** — test NSW locations in 2019-20 fire areas

## Known Limitations

1. **Binary overlays** — VIC BMO is in/out only, no BAL gradient
2. **No QLD data** — returns default
3. **No vegetation density** — overlay maps are coarse, don't capture micro-level vegetation
4. **Static planning overlays** — may lag behind actual vegetation changes

## Future Improvements

- BAL estimation from vegetation type + slope + distance
- Vegetation density from satellite imagery (NDVI)
- Climate change bushfire risk projections
- QLD bushfire mapping (check QSpatial)
