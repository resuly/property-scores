# View Quality Score

Estimates visual amenity potential at a location using five open-data factors.

## Scoring

**Scale**: 0-100 (100 = best views)

| Range | Label |
|-------|-------|
| 85-100 | Exceptional Views |
| 70-84 | Great Views |
| 55-69 | Good Views |
| 40-54 | Average Views |
| 25-39 | Limited Views |
| 0-24 | Obstructed Views |

## Five Factors

### 1. Ocean/Coast Proximity (weight 3.0)

Distance to nearest ocean, bay, or strait feature from Overture water data.

| Distance | Score |
|----------|-------|
| <200m | 1.0 |
| <500m | 0.9 |
| <1km | 0.75 |
| <2km | 0.55 |
| <5km | 0.30 |
| >5km | Linear decay to 0 |

Classes: ocean, sea, bay, strait, tidal_channel, lagoon.

### 2. Inland Water Proximity (weight 1.5)

Distance to nearest river, lake, reservoir, or stream.

| Distance | Score |
|----------|-------|
| <100m | 1.0 |
| <300m | 0.80 |
| <500m | 0.60 |
| <1km | 0.35 |
| <2km | 0.15 |
| >2km | 0 |

Excludes ponds, drains, swimming pools, canals.

### 3. Elevation Advantage (weight 2.5)

Two-scale DEM sampling via Open-Meteo:
- **Near ring**: 8 points at ~500m (detects local hilltops)
- **Far ring**: 8 points at ~2km (detects regional plateaus)

Uses the better of the two advantages. Absolute elevation >50m gets a small bonus.

| Advantage | Score |
|-----------|-------|
| +50m+ | 1.0 |
| +30m | 0.85 |
| +15m | 0.65 |
| +5m | 0.45 |
| 0m | 0.25 |
| Negative | Decays to 0 |

### 4. Green Space (weight 2.0)

Parks, gardens, reserves, playgrounds within 1km from Overture POIs.
Combines proximity (60%) and density (40%).

### 5. Building Openness (weight 2.0)

Inverse of building density within 300m, calibrated for Australian suburbs.

| Buildings in 300m | Score |
|-------------------|-------|
| 0 | 1.0 |
| 10 | 0.95 |
| 40 | 0.80 |
| 100 | 0.65 |
| 200 | 0.45 |
| 350 | 0.25 |
| 400+ | 0.10 |

Tall buildings (>10m) add additional penalty.

## Adaptive Weighting

Factors without available data are excluded rather than penalized.
The final score is: `sum(factor_value * weight) / sum(active_weights) * 100`.

This means:
- Inland locations (no ocean data) are scored on 4 factors
- Remote areas (no POIs) are scored on available factors only

## Data Sources

| Data | Source | Size |
|------|--------|------|
| Water features | Overture Maps (water) | 605 MB (1.1M features) |
| Buildings | Overture Maps (building) | 1.9 GB (13.6M) |
| POIs | Overture Maps (place) | 271 MB (1.4M) |
| Elevation | Open-Meteo DEM API | Real-time |

## Validation Examples

| Location | Score | Label | Notes |
|----------|-------|-------|-------|
| St Kilda | 58 | Good Views | Coastal (341m), dense suburb |
| Mt Dandenong | 67 | Good Views | 577m elevation, +202m advantage |
| Melbourne CBD | 32 | Limited Views | Near bay but tall buildings block |
| Brighton Beach | 50 | Average Views | ON ocean, but flat + suburban |
| Werribee | 40 | Average Views | Near river, typical outer suburb |
| Yarra Valley (rural) | 52 | Average Views | Elevated +21m, very open |

## API

```
GET /scores/view-quality?lat=-37.8676&lng=144.9785
```

Response includes per-factor breakdown for transparency.
