# Property Scores

Open-data property intelligence scoring engine. Computes address-level scores (0-100) for noise, walkability, solar potential, flood risk, bushfire risk, and heat island effect using free government and open datasets.

Built by [Limon Tech](https://limontech.net) as the scoring backend for [DA Leads](https://daleads.com.au).

## Scores

| Score | Status | Data Sources | Method |
|-------|--------|-------------|--------|
| Noise | Live | NFDH AADT + Overture roads/buildings/POIs + State GTFS (6 states, 184 routes) + VicPlan ANEF + NoiseCapture (10K training) | Physics v8 (CRTN + Maekawa + facade) → XGBoost residual → LA50. Test MAE 4.63 dB |
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
| POI | Overture Maps | 1.4M AU-wide | CDLA Permissive |
| Traffic volumes (AADT) | NFDH + WA MRWA | 8,855 stations, 6 states | Open Data |
| Train/tram timetables | State GTFS (VIC/NSW/QLD/WA/SA) | 184 routes, 6 states | CC BY 4.0 |
| Airport noise overlays | VicPlan (DELWP) | Real-time API | CC BY 4.0 |
| Noise ground truth | NoiseCapture crowdsourced | 9,953 AU hexagons (LA50) | ODbL |
| Noise calibration | City of Ballarat fixed sensor | 125K readings | CC BY 3.0 |
| Planning scheme overlays | State governments | Real-time API | Open Data |
| Solar irradiance | Global Solar Atlas | API | CC BY 4.0 |
| Climate data | Open-Meteo (ERA5) | API | CC BY 4.0 |

## Validation

**Coverage**: All Australian states. GTFS rail timetables for VIC/NSW/QLD/WA/SA (184 routes). NFDH AADT for 6 states. Overture roads/buildings/POI AU-wide.

**Production model** (Physics v8 + XGBoost residual → LA50 background noise):
- Trained on 9,953 NoiseCapture real measurements (ODbL, zero legal risk)
- Held-out test (1,991 points): **MAE 4.63 dB, W5 65%, W10 90%**
- Calibrated against Ballarat fixed sensor (125K readings): +3.3 dB vs professional equipment
- Benchmarked against Ambient Maps SoundPLAN (527 buildings): MAX facade MAE 4.5 dB

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
