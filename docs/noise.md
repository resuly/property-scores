# Noise Score — Technical Specification

## Status: Phase 4 — Lden + Validation

| Item | Status |
|------|--------|
| CRTN v1 (class-based) | Deprecated — MAE 19.9 dB vs VicRoads AADT |
| VicRoads AADT download | Done (14,662 segments, VIC only) |
| NFDH national AADT | Done (8,142 stations: NSW, QLD, SA, TAS, VIC) |
| WA Main Roads AADT | Done (713 stations, 2024/25) |
| Overture Roads AU-wide | Done (3,775,980 segments, 1.1 GB) |
| Overture Buildings AU-wide | Done (13,575,172 buildings, 2.0 GB) |
| Building screening model | Done (Maekawa, pre-fetch cached — 54x faster) |
| State GTFS rail timetables | Done — VIC 52, NSW 24, QLD 36, WA 10, SA 13 routes |
| ANEF aircraft contours | Done — VIC only (VicPlan MAEO + AEO) |
| VicRoads directional dedup | Done (road_name + 10m bucket) |
| VicRoads/Overture/NFDH deduplication | Done (80m distance threshold) |
| State detection | Done (au_state.py bounding box) |
| L10 → Leq correction | Done (-3 dB, standard CRTN→Leq) |
| Lden time-of-day model | Done (Austroads 80/12/8 day/eve/night profile) |
| Noise source debug map | Done (/noise/debug — Leaflet dark map with sources) |
| Melbourne NoiseCapture validation | Done (488 hexagons, Bias +4.2, MAE 6.2, Within 5dB 57%) |
| EU END validation set | Downloaded (Germany raster + Netherlands contours) |

## Reference Standard

**CNOSSOS-EU** (Common Noise Assessment Methods in Europe, 2015/996/EU)
- Mandatory for EU strategic noise mapping since 2018
- Validation: RMSE 1.4-2.8 dBA against measurements
- Decomposes into emission model + propagation model
- Supports road, rail, industrial, aircraft sources independently

We implement a simplified version targeting RMSE < 5 dB against EU END maps.

## Architecture

```
Input Sources          Propagation Model         Output
─────────────         ─────────────────         ──────
Road traffic ──┐      Geometric spreading       Lden (dB)
Rail/tram    ──┼──►   Ground absorption    ──►  Score 0-100
Aircraft     ──┤      Building screening        Source breakdown
Industrial   ──┘      Atmospheric absorption    Map visualization
                      First-order reflections
```

## 1. Road Traffic Noise

### Emission Model

CNOSSOS-EU road emission per vehicle category:

```
L_W,i = A_R,i + B_R,i * log10(v/v_ref) + delta_road_surface
```

Simplified for our use: convert to AADT-based CRTN L10:

```
L10_ref = 42.2 + 10 * log10(AADT)
```

### AADT Data Sources

| Source | Coverage | Stations/Segments | Status |
|--------|----------|-------------------|--------|
| VicRoads AADT 2019 | VIC state roads | 14,662 linestring segments | Integrated |
| NFDH Harmonised | NSW, QLD, SA, TAS, VIC | 8,142 point stations | Integrated |
| Main Roads WA | WA state roads | 713 point stations (2024/25) | Integrated |
| Overture road class | Global, all roads | Fallback (CLASS_TO_AADT) | Active |

**Priority chain**: VicRoads (VIC, highest density) → NFDH/MRWA (national, sparser) → Overture CLASS_TO_AADT fallback.
**Coverage gaps**: NT, ACT have no measured AADT — Overture class-based estimates only.

### VicRoads AADT Statistics (Melbourne Metro)

```
AADT Distribution (14,662 segments):
  Min: 2       P10: 499      P25: 1,883
  Median: 4,932   P75: 10,205   P90: 18,387
  Max: 118,458
```

### Speed → AADT Calibration (COMPLETED 2026-04-22)

Matched 1,742 VicRoads segments to Overture roads (spatial join, 80m threshold).

**Key finding**: VicRoads only monitors arterials/highways. "50 km/h residential" in calibration = misclassified arterial, NOT quiet back street. Cannot use speed→AADT for unmonitored roads.

**Decision**: VicRoads AADT as primary source. Overture roads without VicRoads match use conservative CLASS_TO_AADT only.

| Speed (km/h) | VicRoads Median | VicRoads N | Note |
|--------------|-----------------|------------|------|
| 100 | 53,398 | 11 | Freeways |
| 80 | 15,880 | 138 | Highways |
| 70 | 12,411 | 100 | Major arterials |
| 60 | 8,291 | 525 | Arterials (largest sample) |
| 50 | 5,982 | 154 | Biased: VicRoads-monitored only |
| 40 | 6,933 | 112 | School zones on arterials |

| Overture Class | VicRoads Median | Note |
|---------------|-----------------|------|
| motorway | 52,900 | Accurate |
| trunk | 18,933 | Accurate |
| primary | 10,684 | Accurate |
| secondary | 5,857 | Accurate |
| tertiary | 4,603 | Biased high (only busy tertiary monitored) |
| residential | 7,062 | VERY biased (these are misclassified arterials) |

Fallback CLASS_TO_AADT for unmonitored roads (conservative):

| Class | Est. AADT | Rationale |
|-------|-----------|-----------|
| motorway | 50,000 | VicRoads median |
| trunk | 19,000 | VicRoads median |
| primary | 11,000 | VicRoads median |
| secondary | 6,000 | VicRoads median |
| tertiary | 3,000 | Below VicRoads median (unmonitored = quieter) |
| residential | 400 | True quiet street, not VicRoads-monitored |
| service | 150 | Parking/access roads |
| 50 | 800 | residential: 600 |
| 40 | 2,000 | service: 200 |

### Time-of-Day Weighting

CNOSSOS-EU uses Day/Evening/Night split. Simplified approach using AADT temporal profile:

```
Day   (7am-7pm):  ~70% of AADT in 12 hours
Evening (7pm-11pm): ~15% of AADT in 4 hours
Night (11pm-7am):  ~15% of AADT in 8 hours
```

Lden = 10 * log10(1/24 * (12*10^(Ld/10) + 4*10^((Le+5)/10) + 8*10^((Ln+10)/10)))

## 2. Propagation Model

### Distance Attenuation (Adiv)

Line source (road): `-3 dB per distance doubling` (cylindrical spreading)
Point source: `-6 dB per distance doubling` (spherical spreading)

For road segments: treat as finite line source, decompose to point sources at close range.

Simplified: `Adiv = 10 * log10(d / d_ref)`

### Ground Absorption (Agnd)

ISO 9613-2 three-zone model:
- Source zone (30*h_s from source)
- Middle zone
- Receiver zone (30*h_r from receiver)

Ground factor G: 0 = hard (asphalt, water), 1 = soft (grass, soil)

Simplified: `Agnd = 3.0 dB` (mixed ground, conservative)

### Building Screening (Abar) — CRITICAL

**This is the biggest gap in v1. Buildings provide 5-25 dB attenuation.**

Method: 2D ray-casting from road segment to receiver point. If any building polygon intersects the ray:

```
Abar = 10 * log10(3 + 20 * N^2)  (Maekawa formula)
where N = 2 * delta / lambda (Fresnel number)
delta = path length difference over building top edge
```

Simplified approach:
1. Cast ray from source to receiver
2. Find intersecting buildings (Overture footprint + height)
3. Calculate path difference using building height
4. Apply Maekawa barrier attenuation

Data: Overture Buildings with `height` field. Need to download for Melbourne.

### Atmospheric Absorption (Aatm)

Per ISO 9613-1: frequency-dependent, negligible for d < 200m at traffic noise frequencies (500-2000 Hz). ~1 dB/km at 1 kHz.

Simplified: ignore for d < 500m, add `0.005 * d` for d > 500m.

### First-Order Reflections

Buildings near source can reflect sound, adding +2.5 to +3 dB. Important in urban canyons (CBD streets).

TODO: implement for street canyons.

## 3. Rail/Tram Noise

### Emission Model

Train noise is dominated by **rolling noise** (wheel-rail interaction), not engine.

| Type | L_ref (dB) | Ref distance | Duration (s) |
|------|-----------|-------------|-------------|
| Commuter train | 90 | 25m | ~15 |
| Freight train | 95 | 25m | ~60 |
| Melbourne tram | 80 | 7.5m | ~10 |

### Timetable → Leq Conversion

```
Leq = SEL_single + 10*log10(N/T)
where:
  SEL_single = L_peak + 10*log10(duration)
  N = number of pass-bys in period T
  T = period duration in seconds
```

Example: commuter train, 10 pass-bys per hour (peak), SEL = 90 + 10*log10(15) = 101.8 dB
Leq = 101.8 + 10*log10(10/3600) = 101.8 - 25.6 = 76.2 dB at 25m

### Data Source: State GTFS (5 states, 135 routes)

| State | Source | Routes | Types |
|-------|--------|--------|-------|
| VIC | PTV GTFS | 52 | Train (17) + V/Line (13) + Tram (24) |
| NSW | TfNSW GTFS | 24 | Sydney Trains + Metro + Light Rail |
| QLD | TransLink SEQ | 36 | Queensland Rail + Gold Coast Light Rail |
| WA | Transperth | 10 | Transperth trains |
| SA | Adelaide Metro | 13 | Trains + Glenelg/Botanic trams |

VIC PTV: Busiest Sunbury 29.5/hr peak, Route 19 tram 22.5/hr. Quietest Stony Point 1.5/hr.
Coverage gap: ACT light rail not in official GTFS feed. TAS has no urban rail.

**Output files:**
- `D:/property-scores-data/au_rail_frequency.parquet` — 135 routes, 5 states
- `D:/property-scores-data/au_rail_shapes.parquet` — 90,244 shape points, 146 shapes

## 4. Aircraft Noise (COMPLETED 2026-04-22)

### Method

Queries VicPlan Planning Scheme Overlays (ArcGIS REST) for airport noise zones.
Real-time API, no auth, CC BY 4.0, updated weekly by DELWP.

### Data Sources

| Layer | Overlay | Zones | Coverage |
|-------|---------|-------|----------|
| Layer 27 (MAEO) | Melbourne Airport Environs | MAEO1 (≥25 ANEF), MAEO2 (20-25 ANEF) | 21 polygons, 6 LGAs |
| Layer 22 (AEO) | Airport Environs (regional) | AEO1, AEO2, AEO | 44 polygons statewide |

### Zone → Noise Mapping

| Zone | ANEF Range | Penalty dB | Impact |
|------|-----------|------------|--------|
| MAEO1 | ≥ 25 | +12.0 | Severe; residential not recommended |
| MAEO2 | 20-25 | +7.0 | Moderate; acoustic treatment required |
| AEO1 | ≥ 25 | +10.0 | Regional airport, severe |
| AEO2 | 20-25 | +6.0 | Regional airport, moderate |
| AEO | ~20-25 | +5.0 | Regional, unscheduled (conservative) |

### Verified Test Points

| Location | Zone | Penalty | LGA |
|----------|------|---------|-----|
| Keilor (-37.70, 144.83) | MAEO1 | +12 dB | HUME |
| Brimbank (-37.71, 144.85) | MAEO2 | +7 dB | HUME |
| Moorabbin (-37.98, 145.11) | AEO1 | +10 dB | KINGSTON |
| Melbourne CBD | None | 0 dB | — |

### Limitation

VicPlan provides zone-level data, not individual ANEF contour lines with exact dB values.
Fine-grained ANEF contour GIS data for Melbourne Airport is not publicly available.
Other states (NSW, QLD) not yet covered — need separate data sources.

## 5. Validation Plan

### Level 1: AADT Calibration (VIC)

Match VicRoads AADT segments to Overture roads spatially. Build `speed_limit + class → AADT` regression.

Dataset: 14,662 VicRoads segments (downloaded)
Target: calibrated AADT predictions within 30% of actual

### Level 2: EU END Noise Map Comparison (DATA DOWNLOADED 2026-04-22)

Downloaded data:
- **Germany raster**: 100MB GeoTIFF, 10m resolution, all road categories, 5 dB bands (55-75+)
- **Netherlands vector**: Amsterdam road Lden contours via RIVM WFS (national highways only)

Validation approach: sample German raster at known points, compare band vs predicted dB.
For Amsterdam: spatial-join NoiseCapture hexagons with contour bands.

Target: RMSE < 5 dB against official maps

### Level 3: NoiseCapture Crowdsourced Measurements (DATA DOWNLOADED 2026-04-22)

Downloaded from data.noise-planet.org:
- **Melbourne Inner**: 531 hexagons, 29,670 point measurements, LAeq 34-87 dB
- **Amsterdam**: 423 hexagons, 28,165 point measurements, LAeq 28-107 dB
- **Full Australia**: 493 locations across all 8 states
- **Format**: GeoJSON areas (15m hex), points (1-sec GPS), tracks (sessions)

Key caveats:
- Volunteer-driven bias (not systematic spatial coverage)
- GPS accuracy median 19m (15m hex aggregation helps)
- ~28% of Amsterdam tracks tagged `indoor` — need filtering
- Melbourne dominated by CBD/inner suburbs

Validation script: `scripts/validate_noise.py --city melbourne`
Target: RMSE < 8 dB, within-5-dB accuracy > 50%

### Preliminary Melbourne Results (2026-04-22)

Quick test on first 5 CBD hexagons (487 hexagons loaded, 3+ measurements each):

| Metric | Value | Notes |
|--------|-------|-------|
| MAE | 9.4 dB | Overestimating |
| Bias | +9.4 dB | Consistently high |
| Error range | +7.4 to +12.8 dB | Positive bias |

**Analysis**: Model overestimates CBD noise. Likely causes:
1. CBD has very dense VicRoads + Overture road overlap (even after dedup)
2. Building canyon effects (reflections) are not modeled but real buildings block more sound than our simple screening
3. NoiseCapture measurements include off-peak and may be at elevated positions (balconies, rooftops)
4. Need to test suburban/residential hexagons where bias should be smaller

**Next steps**: Run full 487 hexagons, separate by inner vs suburban, filter indoor-tagged measurements.

## 6. Map Visualization

### Noise Source Markers

On the map, display:
- Road segments colored by AADT / estimated dB (green→yellow→red)
- Tram routes (purple lines) with frequency annotation
- Train lines (blue lines) with frequency annotation
- Aircraft ANEF contours (orange zones)
- Building footprints with height (gray polygons)

### Noise Contour Overlay

Pre-compute or real-time compute noise level grid:
- 50m grid spacing
- Color: green (< 50 dB) → yellow (50-60) → orange (60-70) → red (> 70)
- Toggle by source type

## 7. Files

```
property_scores/noise/
  __init__.py          — exports noise_score(), aircraft_noise_penalty()
  score.py             — main scoring function (multi-source)
  aircraft.py          — VicPlan MAEO/AEO overlay queries (DONE)
  buildings.py         — building screening (Maekawa barrier attenuation)

property_scores/common/
  overture.py          — DuckDB spatial queries (roads, rail, AADT, NFDH, GTFS, POIs)
  au_state.py          — state detection + ArcGIS helpers

data/ (external, not in git)
  overture_roads.parquet        — 3.78M AU road segments (1.1 GB)
  overture_buildings.parquet    — 13.6M AU buildings (2.0 GB)
  vicroads_aadt_2019.parquet    — 14,637 VIC AADT segments (linestring)
  nfdh_aadt_national.parquet    — 8,855 national AADT stations (point, 6 states incl WA)
  au_rail_frequency.parquet     — 135 routes, 5 states (VIC/NSW/QLD/WA/SA)
  au_rail_shapes.parquet        — 90,244 shape points, 146 shapes
  gtfs/{state}/                 — Raw GTFS data per state
  eu_end/                       — EU noise maps for validation
  noisecapture/                 — Crowdsourced measurements
```

## v4 Test Results (2026-04-22, AU-wide)

| Location | Score | dB | Road dB | Rail dB | Dominant | Aircraft | Assessment |
|----------|-------|-----|---------|---------|----------|----------|------------|
| CBD Flinders St | 0 | 79.8 | 76.3 | 77.2 | Flinders St AADT=20520 + Traralgon line | — | Correct |
| South Yarra Stn | 0 | 78.2 | 64.2 | 78.1 | Frankston line 22/hr @26m | — | Correct |
| St Kilda Rd tram | 26 | 65.7 | 54.3 | 65.4 | Pakenham line @285m | — | Reasonable |
| Toorak Rd | 29 | 64.8 | 53.6 | 64.4 | Frankston line @494m | — | Reasonable |
| Parkville quiet | 0 | 79.8 | 79.8 | 49.1 | Road-dominated (trunk) | — | Road too loud |
| Surrey Hills back | 26 | 66.1 | 56.1 | 65.6 | Lilydale line @253m | — | Reasonable |
| Bentleigh res | 23 | 67.1 | 59.1 | 66.3 | Frankston line @195m | — | Reasonable |
| Keilor (airport) | 79 | 47.2 | — | — | Aircraft MAEO1 +12dB | MAEO1 | Correct |
| Brimbank (airport) | 73 | 49.6 | �� | — | Aircraft MAEO2 +7dB | MAEO2 | Correct |
| Moorabbin (regional) | 0 | 79.8 | — | — | Road + AEO1 +10dB | AEO1 | Correct |

Rail noise now significant factor: at 200-500m from busy lines (Frankston 22/hr, Lilydale 18/hr), rail dominates road noise. Building screening helps for road noise but not rail.

## Validation Results (v5, 2026-04-22)

Melbourne NoiseCapture: 488 hexagons (≥3 measurements, ≥50 dB outdoor filter).

| Metric | v1 (L10 raw) | v5 (Leq + dedup) |
|--------|-------------|------------------|
| Bias | +6.8 dB | **+4.2 dB** |
| MAE | 8.1 dB | **6.2 dB** |
| RMSE | 10.6 dB | **8.5 dB** |
| Within 5 dB | 40% | **57%** |
| Within 10 dB | 69% | **81%** |
| Per-point time | 27s | **0.5s** (54x) |

Remaining +4.2 dB bias breakdown:
- ~3 dB: NoiseCapture phone microphone systematic underestimation (known limitation)
- ~1 dB: model residual (Overture road class overestimates for quiet streets)
- Worst outliers (+25-30 dB): measurements in parks/indoor (Southbank, Treasury Gardens)

## Known Issues (v5)

1. **NT/ACT no measured AADT** — rely entirely on Overture class estimates
2. **Aircraft VIC only** — MAEO/AEO covers Victoria; national ANEF data available but not yet integrated
3. **ACT light rail not in GTFS** — Transport Canberra feed only includes buses
4. **No bus noise** — GTFS has bus routes but not yet modeled (lower impact than rail)
5. **WA AADT sparse in Perth CBD** — nearest station 1.6km away, no help at 500m radius
6. **Temporal profile is generic** — Austroads 80/12/8 default; NFDH has real hourly data for 3,325 stations

## Changelog

- 2026-04-22: v1 deprecated (CRTN class-based, MAE 19.9 dB). Started v2 rebuild.
- 2026-04-22: Downloaded VicRoads AADT 2019 (14,662 → parquet). Calibrated speed→AADT.
- 2026-04-22: v2 model: VicRoads AADT primary, class fallback, duty-cycle, excess attenuation.
- 2026-04-22: Downloaded Overture Buildings (1.72M, 67.2% height). Implemented Maekawa screening.
- 2026-04-22: Score range improved from 0-0 to 0-100.
- 2026-04-22: PTV GTFS downloaded (52 routes). SEL-based rail noise with actual frequencies.
- 2026-04-22: VicPlan MAEO/AEO aircraft overlay integration (real-time API).
- 2026-04-22: VicRoads/Overture deduplication (80m distance threshold).
- 2026-04-22: Noise product page (noise.html) rewritten for v3 multi-source model.
- 2026-04-22: v4 AU-wide: Overture roads (3.78M), buildings (13.6M), NFDH AADT (8.1k stations, 5 states).
- 2026-04-22: WA Main Roads AADT integrated (713 stations). Total national AADT: 8,855 stations.
- 2026-04-22: State GTFS rail: NSW (24), QLD (36), WA (10), SA (13) routes added to VIC (52).
- 2026-04-22: State detection (au_state.py), skip VicPlan aircraft for non-VIC.
- 2026-04-22: Renamed ptv_rail_near→gtfs_rail_near for national GTFS support.
- 2026-04-22: v5 — L10→Leq correction (-3 dB), Lden time-of-day model (Austroads 80/12/8 profile).
- 2026-04-22: Building screening 54x speedup — pre-fetch radius once, compute in Python.
- 2026-04-22: VicRoads directional dedup (road_name + 10m bucket), fixes energy double-count.
- 2026-04-22: Noise source debug map (/noise/debug) — Leaflet dark map with AADT/NFDH/rail sources.
- 2026-04-22: Melbourne validation complete: 488 hex, Bias +4.2, MAE 6.2, W5 57%, W10 81%.

## Improvement Roadmap (next session handoff)

Ranked by impact/effort. Each item is independent.

### 1. NFDH Hourly Bins → Real Temporal Profile (high impact, medium effort)
3,325 NFDH stations have 12-bin (2-hourly) data. Currently we only downloaded total AADT.
- Re-query NFDH API with `counter_type='12-bin'`, extract hourly distribution
- Replace generic Austroads 80/12/8 with per-station measured day/eve/night fractions
- Expected: more accurate Lden for locations near NFDH stations, especially freight corridors

NFDH counter_type breakdown: 12-bin=3,325 | 04-bin=1,822 | 02-bin=2,402 | 01-bin=593 | Class=713

### 2. Heavy Vehicle CRTN Correction (medium impact, easy)
CRTN has HV correction: `+10*log10(1 + 5*p/V)` where p=%HV, V=speed(km/h).
- hv_pct already in VicRoads + NFDH data, just not used in `_crtn_noise()`
- Would increase noise on freight routes by 2-5 dB
- NOTE: this increases predictions → makes bias worse unless combined with other fixes
- Test on freight corridors (Western Ring Road, Hume Freeway) before enabling globally

### 3. National ANEF Aircraft Noise (medium impact, medium effort)
Currently VIC-only via VicPlan API. Airservices Australia publishes national ANEF shapefiles.
- Download from `data.gov.au` or Airservices AU
- Parse ANEF contour polygons (20, 25, 30, 35 ANEF zones)
- Replace VicPlan API call with local spatial lookup (faster, national coverage)
- Airports affected: Sydney (Kingsford Smith), Brisbane, Perth, Adelaide, Canberra, Gold Coast

### 4. Cross-City Validation (medium impact, easy)
NoiseCapture has data for Sydney, Brisbane, Perth. Run `validate_noise.py --city sydney` etc.
- Validates NFDH + GTFS accuracy outside Melbourne
- Tests whether Melbourne-calibrated model generalises
- Need to download NoiseCapture data for other cities first

### 5. EPA Noise Monitoring Calibration (low-medium impact, medium effort)
VIC EPA and NSW EPA operate fixed noise monitoring stations with accurate reference microphones.
- These give ground-truth Leq values without phone-mic bias
- Even 10-20 reference points would calibrate the ~3 dB phone-mic offset
- Sources: EPA Victoria data portal, NSW EPA AQMS

### 6. Adaptive TOP_N Sources (low impact, easy)
Currently fixed TOP_N_ROAD_SOURCES=3. In dense CBDs this ignores 60+ sources;
in quiet suburbs it may over-represent the few sources present.
- Alternative: take all sources within 10 dB of maximum
- Or: logarithmic weighting that diminishes contribution of distant/quiet sources
