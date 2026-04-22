# Noise Score — Technical Specification

## Status: Phase 1 — Rebuilding

| Item | Status |
|------|--------|
| CRTN v1 (class-based) | Deprecated — MAE 19.9 dB vs VicRoads AADT |
| VicRoads AADT download | Done (14,662 segments, 32MB GeoJSON → parquet) |
| AADT spatial calibration | Done (1,742 matches, see Speed→AADT section) |
| VicRoads AADT as primary source | Done (v2 model uses real AADT) |
| Overture Buildings download | Done (1.72M buildings, 67.2% with height, 175.8 MB) |
| Building screening model | Done (Maekawa formula, 13-20 dB detected, ~1-5 dB score impact) |
| PTV GTFS rail timetable | TODO |
| ANEF aircraft contours | TODO |
| EU END validation set | TODO |
| NoiseCapture validation set | TODO |

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

| Source | Coverage | Resolution | Status |
|--------|----------|------------|--------|
| VicRoads AADT 2019 | VIC state roads | 14,662 segments with exact volumes | Downloaded |
| Overture speed_limits | Global, 22.6% coverage | Posted limit only, no volume | Available |
| Overture road class | Global, all roads | Classification unreliable (MAE 19.9 dB) | Available |

**Priority**: Use VicRoads AADT directly for VIC. For roads without AADT, use calibrated speed_limit→AADT mapping (calibration in progress).

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

### Data Source: PTV GTFS

PTV publishes GTFS feed with stop_times for all train/tram/bus routes. From this we can extract:
- Number of services per hour per route
- Peak vs off-peak frequencies
- Route geometry (from shapes.txt)

URL: https://www.ptv.vic.gov.au/footer/data-and-reporting/datasets/

## 4. Aircraft Noise

### Method

Use pre-computed ANEF (Australian Noise Exposure Forecast) contours. These are created by Airservices Australia using the AEDT model and account for:
- Flight path geometry
- Aircraft type mix
- Runway usage patterns
- Projected movements

### Data Sources

| Airport | Data | Format |
|---------|------|--------|
| Melbourne (MEL) | MAEO overlay in VicPlan | ArcGIS REST |
| Sydney (SYD) | ANEF 2039 in master plan | PDF/shapefile |
| Defence airfields | ANEF layers | data.gov.au GeoJSON |
| EU airports | END noise contours | EEA Datahub |

For Melbourne: query VicPlan ArcGIS for Melbourne Airport Environs Overlay (MAEO) which defines noise-affected zones.

### Score Impact

ANEF 20+ = significant aircraft noise → score penalty
ANEF 25+ = severe → large penalty
ANEF 30+ = extreme → near-zero noise score

## 5. Validation Plan

### Level 1: AADT Calibration (VIC)

Match VicRoads AADT segments to Overture roads spatially. Build `speed_limit + class → AADT` regression.

Dataset: 14,662 VicRoads segments (downloaded)
Target: calibrated AADT predictions within 30% of actual

### Level 2: EU END Noise Map Comparison

Download Lden contours for a European city (e.g., Paris or Berlin).
Run our model on the same city using OSM data.
Compare predicted vs official noise levels at grid points.

Dataset: EEA Datahub (GeoPackage format)
Target: RMSE < 5 dB

### Level 3: NoiseCapture Global Measurements

Download crowdsourced noise measurements from noise-planet.org.
For each measurement point with GPS, run our model.
Compare predicted vs measured LAeq.

Dataset: data.noise-planet.org (GeoJSON, ODbL license)
Target: correlation > 0.7, RMSE < 8 dB

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
  __init__.py          — exports noise_score()
  score.py             — main scoring function (multi-source)
  propagation.py       — CNOSSOS-EU simplified propagation model (TODO)
  road_emission.py     — road traffic emission model (TODO)
  rail_emission.py     — rail/tram emission from GTFS (TODO)
  aircraft.py          — ANEF overlay query (TODO)
  buildings.py         — building screening (Maekawa barrier attenuation)
  calibration.py       — VicRoads AADT calibration (TODO)

data/ (external, not in git)
  overture_roads.parquet        — 592k Melbourne road segments
  overture_buildings.parquet    — 1.72M Melbourne buildings (67.2% with height)
  vicroads_aadt_2019.geojson    — 14,662 AADT segments (raw)
  vicroads_aadt_2019.parquet    — 14,637 AADT segments (spatial query ready)
  ptv_gtfs/                     — PTV timetable data (TODO download)
  eu_end/                       — EU noise maps for validation (TODO download)
  noisecapture/                 — Crowdsourced measurements (TODO download)
```

## v2 Test Results (2026-04-22)

| Location | Score | dB | AADT# | Dominant | Screening | Assessment |
|----------|-------|-----|-------|----------|-----------|------------|
| CBD Flinders St | 0 | 79.0 | 57 | Flinders St AADT=20520 @80m | 20dB (secondary) | Correct |
| South Yarra Stn | 0 | 76.8 | 15 | primary @45m | 20dB | Correct |
| St Kilda Rd | 0 | 79.1 | 10 | trunk @40m | 20dB | Correct |
| Toorak Rd | 2 | 74.2 | 9 | primary @69m | 20dB | Correct |
| Parkville quiet | 39 | 61.5 | 2 | trunk @255m | 13.9dB | Reasonable |
| Surrey Hills back | 14 | 70.2 | 2 | Canterbury Rd @103m | 20dB | Correct |
| Bentleigh res | 45 | 59.1 | 0 | secondary @175m | 0dB | Reasonable |
| Malvern deep res | 21 | 67.7 | 16 | Dandenong Rd @174m | 20dB | Reasonable |
| Vermont deep res | 32 | 63.6 | 0 | secondary @122m | 0dB | Reasonable |
| Doncaster back | 42 | 60.4 | 4 | Doncaster Rd @221m | 20dB | Reasonable |
| Eltham semi-rural | 37 | 62.1 | 2 | tertiary @77m | 16.8dB | Reasonable |

Score range: 0-100 (was 0-0 in v1). Building screening detected but has modest impact (~1-5 dB) because dominant source (nearest road) is rarely screened.

## Known Issues (v2)

1. **Screening only helps secondary sources** — nearest road is in line-of-sight, buildings behind you don't help
2. **VicRoads coverage gaps** — some freeways/motorways missing from AADT data
3. **Overture class still used as fallback** — trunk=19000 can override actual VicRoads data
4. **Query time 0.4-4.6s** — building intersection queries on 1.7M buildings are expensive
5. **No rail timetable** — fixed Leq estimate instead of actual frequency

## Changelog

- 2026-04-22: v1 deprecated (CRTN class-based, MAE 19.9 dB). Started v2 rebuild.
- 2026-04-22: Downloaded VicRoads AADT 2019 (14,662 → parquet). Calibrated speed→AADT.
- 2026-04-22: v2 model: VicRoads AADT primary, class fallback, duty-cycle, excess attenuation.
- 2026-04-22: Downloaded Overture Buildings (1.72M, 67.2% height). Implemented Maekawa screening.
- 2026-04-22: Score range improved from 0-0 to 0-100. Remaining: PTV GTFS, ANEF, validation.
