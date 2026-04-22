# Property Scores

Open-data property intelligence scoring engine. Computes address-level scores (0-100) for noise, walkability, solar potential, flood risk, bushfire risk, and heat island effect using free government and open datasets.

Built by [Limon Tech](https://limontech.net) as the scoring backend for [DA Leads](https://daleads.com.au).

## Scores

| Score | Status | Data Sources | Method |
|-------|--------|-------------|--------|
| Noise | Live | NFDH + VicRoads AADT + WA MRWA + State GTFS (5 states) + Overture roads/buildings + VicPlan ANEF | CRTN L10 + SEL rail + Maekawa screening + ANEF aircraft |
| Walkability | Live | Overture POI (227k Melbourne) | Distance-decay across 13 categories |
| Solar Potential | Live | Global Solar Atlas API | GHI/DNI + orientation + tilt |
| Flood Risk | Live | State government planning overlays (VIC/NSW/SA/TAS/ACT) | ArcGIS REST point-in-polygon |
| Bushfire Risk | Live | State government overlays (VIC/NSW/WA/SA/TAS) | BMO/BPL severity classification |
| Heat Island | Live | Open-Meteo ERA5 + Overture buildings/POI | 5-year summer P90 + density + green space |

## Quick Start

```bash
pip install -e .

# Single score
python -m property_scores.noise.score --lat -37.8136 --lng 144.9631

# API server
uvicorn property_scores.api.main:app --host 0.0.0.0 --port 8099
# Then visit http://localhost:8099
```

## Architecture

```
property_scores/
  noise/            # Multi-source: road AADT + rail GTFS + aircraft ANEF + building screening
  walkability/      # Walk Score-style POI distance analysis
  solar/            # Solar irradiance via Global Solar Atlas
  flood/            # State planning scheme flood overlays
  bushfire/         # State planning scheme bushfire overlays
  heat_island/      # Summer temperature + urban density
  common/           # DuckDB spatial, Overture loaders, config
  api/              # FastAPI + product pages
```

## Data Dependencies

All data is free and open-licensed:

| Dataset | Provider | Size | License |
|---------|----------|------|---------|
| Road network | Overture Maps | 3.78M AU segments | CDLA Permissive |
| Building footprints + heights | Overture Maps | 13.6M AU buildings | CDLA Permissive |
| POI | Overture Maps | 227k Melbourne | CDLA Permissive |
| Traffic volumes (AADT) | VicRoads + NFDH + MRWA | 23k segments/stations | Open Data |
| Train/tram timetables | State GTFS (VIC/NSW/QLD/WA/SA) | 135 routes, 5 states | CC BY 4.0 |
| Airport noise overlays | VicPlan (DELWP) | Real-time API | CC BY 4.0 |
| Planning scheme overlays | State governments | Real-time API | Open Data |
| Solar irradiance | Global Solar Atlas | API | CC BY 4.0 |
| Climate data | Open-Meteo (ERA5) | API | CC BY 4.0 |

## Validation

**Coverage**: All Australian states. VIC has highest accuracy (VicRoads AADT + PTV GTFS). NSW, QLD, SA, TAS, WA have NFDH/MRWA AADT + state GTFS. NT/ACT use Overture road class estimates.

Noise model validated against:
- VicRoads AADT (1,742 spatial matches, calibration data)
- NoiseCapture crowdsourced measurements (Melbourne 531 hexagons, Amsterdam 423 hexagons)
- EU END strategic noise maps (Germany 10m raster, Netherlands contours)

See `docs/noise.md` for full methodology and validation results.

## API Endpoints

```
GET /scores?lat=-37.8136&lng=144.9631          # All 6 scores
GET /scores/noise?lat=-37.8136&lng=144.9631     # Noise only
GET /scores/walkability?lat=-37.8136&lng=144.9631
GET /scores/solar?lat=-37.8136&lng=144.9631
GET /scores/flood?lat=-37.8136&lng=144.9631
GET /scores/bushfire?lat=-37.8136&lng=144.9631
GET /scores/heat-island?lat=-37.8136&lng=144.9631
GET /scores/aircraft-noise?lat=-37.70&lng=144.83
```

## License

MIT
