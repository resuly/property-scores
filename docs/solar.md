# Solar Score — Technical Specification

## Status: v1 Stable

| Item | Status |
|------|--------|
| Global Solar Atlas API | Done |
| Basic scoring (GHI/DNI/PVout) | Done |
| Roof area estimation | Done (default 50m2) |
| Product page | Done (solar.html) |
| Validation vs BOM gridded data | TODO |

## Data Source

**Global Solar Atlas** (World Bank / Solargis)
- API: `https://api.globalsolaratlas.info/data/lta`
- Coverage: Global, ~1km resolution
- Parameters: GHI, DNI, DIF, GTI, PVOUT, OPTA, temperature, elevation
- License: CC BY 4.0

## Scoring Method

```
PVout range: 800 (poor) to 1800 (excellent) kWh/kWp/year
Score = clamp((pvout - 800) / (1800 - 800) * 100, 0, 100)
```

Labels:
- 80+: Excellent Solar Potential
- 60-79: Good Solar Potential
- 40-59: Moderate Solar Potential
- 20-39: Below Average
- 0-19: Poor Solar Potential

## Output Fields

- score (0-100)
- label
- ghi_kwh_m2_year
- dni_kwh_m2_year
- pvout_kwh_kwp_year
- optimal_tilt_deg
- temp_avg_c
- elevation_m
- estimated_annual_kwh (if roof_area provided)

## Validation Plan

Compare against:
1. **BOM Solar Exposure gridded data** (Australia): monthly mean daily solar exposure
2. **PVGIS** (EU Joint Research Centre): independent PV estimation tool
3. **Actual rooftop PV production data** (if available via Solar Analytics or similar)

Target: within 10% of BOM/PVGIS values.

## Accuracy Assessment

Estimated accuracy: **90%** — Global Solar Atlas is well-validated, satellite-derived data. Main limitation is no local shading (trees, buildings), which would require 3D analysis.

## Future Improvements

- 3D shadow analysis using building/vegetation data (high effort)
- Roof orientation detection from building footprint (medium effort)
- Integration with electricity pricing for financial return estimate
