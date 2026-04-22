# Walkability Score — Technical Specification

## Status: v1 Functional

| Item | Status |
|------|--------|
| Overture POI categories | Done (14 categories) |
| Distance-weighted scoring | Done |
| Product page | Done (walkability.html) |
| Validation vs Walk Score | TODO |
| Network distance (vs straight-line) | TODO |

## Data Source

**Overture Maps Places** (via local parquet)
- File: `overture_pois.parquet` (227,646 Melbourne POIs, 41.7 MB)
- Categories: primary category from Overture schema
- Coverage: Melbourne metro (global data available)

## Scoring Method

14 walkability categories, each with a weight and ideal distance:

| Category | Keywords | Weight | Ideal (m) | Max (m) |
|----------|----------|--------|-----------|---------|
| Grocery | supermarket, grocery | 3.0 | 400 | 1500 |
| Restaurant/Cafe | restaurant, cafe, coffee | 2.0 | 500 | 1500 |
| School | school, education | 2.5 | 800 | 1500 |
| Park | park, garden, recreation | 2.0 | 400 | 1500 |
| Public Transport | bus, train, tram, station | 3.0 | 400 | 1500 |
| Medical | hospital, medical, pharmacy | 2.0 | 800 | 1500 |
| Bank/ATM | bank, atm | 1.0 | 800 | 1500 |
| Shopping | shop, retail, mall | 1.5 | 500 | 1500 |
| Gym/Sports | gym, fitness, sport | 1.0 | 800 | 1500 |
| Library | library | 1.0 | 1000 | 1500 |
| Childcare | childcare, kindergarten | 2.0 | 800 | 1500 |
| Post Office | post office | 0.5 | 1000 | 1500 |
| Community | community, church, temple | 1.0 | 800 | 1500 |
| Entertainment | cinema, theatre, museum | 1.0 | 1000 | 1500 |

Per-category score: `weight * max(0, 1 - distance/max_distance)`

Total: weighted sum normalized to 0-100.

## Output Fields

- score (0-100)
- label
- poi_count (total POIs found in radius)
- category_scores (per-category: nearest distance, score, POI name)

## Validation Plan

1. **Walk Score API** — compare our scores to walkscore.com for 100 random Melbourne addresses. Target: correlation > 0.7
2. **Manual spot-check** — 20 locations with known walkability (CBD vs outer suburb vs rural)
3. **AURIN walkability index** — if available, compare against academic walkability datasets

## Known Limitations

1. **Straight-line distance** — real walking routes may be 30-50% longer due to street network. Network distance would require routing (e.g., OSRM).
2. **POI quality** — Overture Places has gaps. Some categories (childcare, post office) have low coverage.
3. **No pedestrian infrastructure** — doesn't consider footpath quality, crossings, lighting.

## Future Improvements

- Network distance using OSRM or Valhalla
- Pedestrian infrastructure quality (OSM footway data)
- Time-based walkability (opening hours from POIs)
- Public transport accessibility score (frequency + travel time to CBD)
