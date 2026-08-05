# Heat Island Score — Technical Specification

> 以代码为准 (`property_scores/heat_island/score.py`)。本 doc 2026-07-02 与代码对齐;
> 历史版本曾写 TEMP 22/35、MODIS "TODO"、density×15,均已过时,勿引用旧数。

## Status: v2 (local MODIS mosaic)

| Item | Status |
|------|--------|
| MODIS LST 1km surface temp (day + night) | Done — **local summer mosaic** (`data/global/modis_lst_*.vrt`) |
| Open-Meteo ERA5 5-year summer temp (fallback) | Done |
| Building density (Overture Buildings, 500m) | Done |
| Greenspace (ESA WorldCover green fraction 500m, POI fallback) | Done |
| Anchor calibration (350-pt AU sweep + 18 truth anchors) | Done (2026-06-11 reanchor) |
| Validation vs Melbourne UHI dataset | TODO |

## Data Sources

| Data | Source | Resolution | Status |
|------|--------|-----------|--------|
| Surface LST (day + night) | **Local MODIS 11A2 summer mosaic** (baked from Planetary Computer, `scripts/download_modis_lst.py`) | 1km | Local — sampled via `common.landcover.sampler` |
| Summer air temperature (fallback) | Open-Meteo Historical API (ERA5) | ~10km grid | Remote, ~1.2s |
| Building density | Overture Buildings (500m radius) | Building-level | Local parquet |
| Green space | ESA WorldCover green fraction (500m); Overture park POIs fallback | 10m | Local VRT |

### Local MODIS mosaic (2026-07-02)

The remote signed-COG path was **17.7s of a 19.7s cold call** (STAC search + SAS
signing + remote range reads over 25 samples). `scripts/download_modis_lst.py`
bakes the same signal into a local sinusoidal mosaic — per MODIS tile, the summer
(Dec–Feb) 8-day composites are combined with a per-pixel **median** (invalid/fill
pixels dropped), day and night each written to a GeoTIFF, mosaicked with
`gdalbuildvrt` into `data/global/modis_lst_day.vrt` / `modis_lst_night.vrt`.
Median (not mean) resists tropical wet-season cloud contamination: a mean drags
Darwin-type points ~1.8C low and flips the label Hot→Moderate, median holds the
clear-sky value (verified against `_modis_lst_remote`).

`_modis_lst()` now samples those VRTs through the shared `raster_sample` sampler
(which reprojects lat/lng into the MODIS sinusoidal CRS automatically, same as the
DEM/WorldCover layers). Point pixel = point LST; the mean over a 2km window (the
same 5×5 1km-pixel neighbourhood) = area LST for the UHI delta. Outside tile
coverage or on a water pixel it returns NODATA → None → ERA5 fallback, exactly as
a remote MODIS miss behaved. The original remote implementation is retained as
`_modis_lst_remote()` (source-of-truth reference for the mosaic + optional slow
fallback; not on the hot path). Cold call drops from ~19.7s to ~1.2s.

## Scoring Method

```python
# Absolute surface temperature (TEMP_COOL = 25.0, TEMP_HOT = 45.0)
temp_score = clamp((TEMP_HOT - lst) / (TEMP_HOT - TEMP_COOL) * 100, 0, 100)

# UHI penalty: hotter than the 5x5 surroundings (skipped where >30% of the 500m
# surround is water/tree, so coastal/forest-edge points are not mis-penalised)
uhi_penalty = max(0, uhi_delta) * 3
# + night heat-retention penalty when night LST > 18C: min((night - 18) * 1.5, 10)

# Local adjustments
density_penalty = building_density_0to1 * 6     # Overture, 500m
green_bonus     = greenspace_0to1 * 5           # WorldCover green fraction, 500m

score = clamp(temp_score - uhi_penalty - density_penalty + green_bonus, 0, 100)
```

ERA5 fallback (no MODIS): measures air temp, so an air→LST offset is applied
(`effective_temp = mean*0.4 + p90*0.6 + 6.0`) and the payload is marked
low-confidence (`source: era5`, `confidence_note`).

Labels: `>=85` Very Cool · `>=60` Cool · `>=40` Moderate Heat · `>=20` Hot · `<20` Extreme Heat.

## Output Fields

- `score` (0-100, 100 = coolest), `label`, `source` (`modis` | `era5`)
- `modis_lst_c`, `night_lst_c`
- `uhi_delta_c` — omitted where the surround is sea/forest, and on the
  borrowed-pixel path below
- `modis_area_c` — still emitted on the sea/forest path, omitted on the
  borrowed-pixel path (DA Leads' map computes its own delta from
  `modis_lst_c - modis_area_c`, so leaving it there would render the very
  comparison that path says it cannot make)
- `lst_source` — `pixel` (the address's own 1 km pixel) or `nearest_land_pixel`
- `lst_offset_m`, `lst_pixels_averaged` — borrowed-pixel path only: distance to
  the ring that carried data, and how many pixels of it were averaged
- `summer_mean_c`, `summer_p90_c` (ERA5)
- `building_density` (0-1), `greenspace_factor` (0-1)
- `confidence_note` (only when `source == era5`)

## Known Limitations

1. **1km LST resolution** — intra-block variation below 1km is not resolved; the
   Tarneit-vs-Kew truth anchor is only 1.7C apart at sensor resolution.
2. **Water-pixel dropout** — waterfront addresses whose 1km sinusoidal pixel is
   water-masked have no reading of their own. Since 2026-08-05 the value comes
   from the nearest ring of pixels that do have data, within 2km, reported via
   `lst_source` / `lst_offset_m` / `lst_pixels_averaged` and stated in the
   disclaimer; the UHI term and `modis_area_c` are withheld. Measured over 6000
   AU DA coordinates: 152 addresses had no reading of their own, 149 recovered
   (142 at 927m, 7 at 1310m) and 3 stayed "Data unavailable". The ERA5 fallback
   this used to mention was removed 2026-08-02 (non-commercial-use terms).
3. **No impervious-surface / wind-corridor / canyon modelling** — density and
   greenspace are proxies for these.
4. **Mosaic vintage** — the local mosaic is a fixed summer median; refresh by
   re-running `scripts/download_modis_lst.py` (add `--seasons 2022,2023,2024` for
   a multi-year median).
5. **UHI delta from a pre-aggregated mosaic** — the point-vs-area delta is taken
   on the median mosaic. This is *more* stable than the remote path's 3-composite
   delta (a genuine-UHI point read remote uhi 3.7 from 3 composites vs a robust
   ~1.5 over 19), but the median softens the spatial gradient by ~0.5-1C. Absolute
   LST and labels were unaffected across the 18 truth anchors.

## Validation

- `scripts/validate_heat_local.py` — runs the 18 truth anchors
  (`data/truth_anchors/heat_island_anchors.csv`) through the local scorer, checks
  each expected gate, and diffs point LST against `_modis_lst_remote` to prove the
  local mosaic is lossless.
- `scripts/anchor_sweep.py` — stratified AU sample for scale re-anchoring.
- TODO: correlation vs Melbourne UHI Dataset 2018 / BOM stations (target > 0.7).

## Future Improvements

- NDVI raster for vegetation density (better than POI count — partly addressed by WorldCover).
- Impervious-surface fraction; wind-corridor and building-height canyon modelling.
- Multi-year mosaic + per-summer trend.
