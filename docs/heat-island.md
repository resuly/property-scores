# Heat Island Score — Technical Specification

## Status: v1 Functional (Low Accuracy)

| Item | Status |
|------|--------|
| Open-Meteo 5-year summer temp | Done |
| Building density proxy | Done (needs Overture Buildings) |
| Greenspace proxy | Done (Overture POIs) |
| Product page (heat_island.html) | TODO |
| Validation vs Melbourne UHI dataset | TODO |
| Overture Buildings download | TODO (shared with Noise) |

## Data Sources

| Data | Source | Resolution | Status |
|------|--------|-----------|--------|
| Summer temperature | Open-Meteo Historical API | ~10km grid (ERA5 reanalysis) | Available |
| Building density | Overture Buildings | Building-level | TODO download |
| Green space | Overture POIs (parks/gardens) | POI-level | Available |
| Satellite LST | Landsat/MODIS | 30m-1km | TODO (best ground truth) |

## Scoring Method

```python
# Temperature component (60% weight)
effective_temp = mean_summer_max * 0.4 + p90_summer_max * 0.6
temp_score = (TEMP_HOT - effective_temp) / (TEMP_HOT - TEMP_COOL) * 100
# TEMP_COOL = 22.0, TEMP_HOT = 35.0

# Building density component (penalty up to -15 points)
density_penalty = building_density_0to1 * 15

# Greenspace component (bonus up to +5 points)
green_bonus = greenspace_factor_0to1 * 5

score = clamp(temp_score - density_penalty + green_bonus, 0, 100)
```

## Output Fields

- score (0-100, 100 = coolest)
- label (Very Cool / Cool / Moderate Heat / Hot / Extreme Heat)
- summer_mean_c (5-year summer mean daily max)
- summer_p90_c (5-year P90 daily max)
- building_density (0-1, if building data available)
- greenspace_factor (0-1, if POI data available)

## Validation Plan

1. **Melbourne UHI Dataset 2018** (data.vic.gov.au) — satellite-derived land surface temperature map
2. **BOM station data** — compare Open-Meteo vs actual BOM weather station records
3. **Landsat LST** — urban heat island detection from thermal infrared bands
4. **AURIN urban heat data** — if available

Target: correlation > 0.7 with satellite LST data.

## Known Limitations

1. **Coarse temperature grid** — Open-Meteo uses ERA5 (~10km). Urban heat island effects are at 100m-1km scale. All locations within a grid cell get same base temperature.
2. **Building density not available** — Overture Buildings not downloaded yet. Returns N/A.
3. **No impervious surface data** — asphalt/concrete coverage is the primary UHI driver, not directly measured.
4. **No wind/ventilation** — sea breeze and wind corridors significantly affect urban heat.

## Estimated Accuracy

**50%** — temperature grid is too coarse to capture intra-urban variation. Building density and greenspace adjustments help but are crude proxies.

## Future Improvements

- Satellite LST data (Landsat thermal band, 30m resolution)
- NDVI for vegetation density (much better than POI count)
- Impervious surface fraction from land use data
- Wind corridor analysis
- Building height → canyon effect modeling
