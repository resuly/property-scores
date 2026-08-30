# Property Scores

Open-data property intelligence scoring engine. Computes address-level scores (0-100) for noise, walkability, solar potential, flood risk, bushfire risk, and heat island effect using free government and open datasets.

Built by [Limon Tech](https://limontech.net) as the scoring backend for [DA Leads](https://daleads.com.au).

## Scores

| Score | Status | Data Sources | Method |
|-------|--------|-------------|--------|
| Noise | Live | NFDH AADT + Overture roads/buildings/POIs + State GTFS (6 states, 184 routes) + VicPlan ANEF + EU national noise maps (NL RIVM + UK DEFRA) for training | `eu-transfer-v1`: geometry-only RF trained on EU maps → per-state constrained-slope affine calibration → LA50. Gate r 0.696 / MAE 3.798 (SoundPLAN in-city 5-fold, 5 seeds). **Against real instruments (199 EIS points): MAE 7.98 dB, systematically hot +6.4 dB.** See `limon-ops/logs/da-leads/noise-model.md` (SSOT) |
| Walkability | Live | Overture POI (227k Melbourne) | Distance-decay across 13 categories |
| Solar Potential | Live | Global Solar Atlas API | GHI/DNI + orientation + tilt |
| Flood Risk | Live | State government planning overlays (VIC/NSW/SA/TAS/ACT) | ArcGIS REST point-in-polygon |
| Bushfire Risk | Live | State government overlays (VIC/NSW/WA/SA/TAS) | BMO/BPL severity classification + indicative BAL pre-screen (AS 3959 Method 1, see docs/bal-prescreen.md) |
| Contamination Screening | Live map; Self-Serve API not ready | VIC/NSW/ACT official registers, VIC EPA Environmental Audit/history/landfill/groundwater context, SA/QLD/TAS context, national landfill/industrial context | On-site-first evidence screen; audit locations are evidence-only and never prove contamination; optimistic scores withheld when required coverage is incomplete; not a clean-site certificate or environmental assessment |
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
| Noise main gate (instruments) | EIS report noise loggers | 199 points, 5 state groups | report-derived |
| Noise A/B aid (model output, NOT measurement) | SoundPLAN simulated facade Lden | 11,015 points, 7 cities | see SSOT |
| Noise ground truth (RETIRED model only) | NoiseCapture crowdsourced | 9,953 AU hexagons (LA50) | ODbL |
| Noise calibration (do not use as a gate) | City of Ballarat fixed sensor | 125K readings, but only **3 coordinates** (spatial n≈1) | CC BY 3.0 |
| Planning scheme overlays | State governments | Real-time API | Open Data |
| Solar irradiance | Global Solar Atlas | API | CC BY 4.0 |
| Climate data | Open-Meteo (ERA5) | API | CC BY 4.0 |

## Validation

**Coverage**: All Australian states. GTFS rail timetables for VIC/NSW/QLD/WA/SA (184 routes). NFDH AADT for 6 states. Overture roads/buildings/POI AU-wide.

**Production model** = `eu-transfer-v1` (active in `data/models/noise/registry.json`; live since 2026-06-08):
geometry-only RF trained on EU national noise maps (NL RIVM + UK DEFRA) → per-state
constrained-slope affine calibration → aircraft ANEF remix → quiet-end physics blend → loud-end reanchor.
- Gate (`scripts/calib_eval.py`, SoundPLAN in-city 5-fold, MIN_LDEN=30): **r 0.696 / MAE 3.798 dB**, mean of 5 seeds
- **Against real instruments** (`scripts/eis_measured_gate.py`, 199 EIS logger points): **MAE 7.98 dB, bias +6.4 dB,
  positive in all 5 state groups (+2.8 to +9.5)** — the model reads systematically hotter than measured
- Free-data ceiling is r 0.68–0.70; Mapbox / measured-AADT / US-training / AlphaEarth were all tried and judged negative
- Positioning: desktop **pre-screen only**, never compliance grade (the regulatory metric is LA90, we output LA50)

⚠️ **History, not current** — an earlier model (2026-04-22, Physics v8 + XGBoost residual) was trained on
9,953 NoiseCapture crowdsourced hexagons and scored MAE 4.63 dB on a 1,991-point held-out test.
**That model is retired** (`property_scores/noise/distill.py` calls it "the (retired) crowdsourced-NoiseCapture model"),
and NoiseCapture was later measured at bias −14.8 dB / r = −0.12 against our engine, i.e. no discriminative power.
**Do not quote 9,953 or 4.63 dB as current capability** — that mistake reached an internal opportunity register in
2026-07 and had to be corrected twice (`limon-ops/logs/venture/2026-07-26_asset-claim-audit.md`).

Single source of truth for anything about this model: `limon-ops/logs/da-leads/noise-model.md`.

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
