# Property Scores

Open-data property intelligence scoring engine. Computes address-level scores (0–100) for noise, walkability, solar potential, flood risk, and more — using free global datasets (Overture Maps, Sentinel-2, Copernicus DEM, Global Solar Atlas).

Built by [Limon Tech](https://limontech.net) as the scoring backend for [DA Leads](https://daleads.com.au).

## Scores

| Score | Status | Data Sources | Method |
|-------|--------|-------------|--------|
| Noise | Phase 1 | Overture roads + buildings | CRTN formula + energy summation |
| Walkability | Planned | Overture POI + OSM amenities | Distance-decay across 13 categories |
| Solar Potential | Planned | Global Solar Atlas + Overture buildings | GHI × roof area × orientation |
| Flood Risk | Planned | JRC Surface Water + Copernicus DEM | HAND + historical occurrence |
| Bushfire Risk | Planned | Sentinel-2 NDVI + NASA FIRMS + DEM | Multi-factor composite |

## Quick Start

```bash
pip install -e .
python -m property_scores.noise --lat -37.8136 --lng 144.9631
```

## Architecture

```
property_scores/
├── noise/          # Noise score (CRTN + ML)
├── walkability/    # Walk Score-style POI analysis
├── solar/          # Solar potential (GHI + building orientation)
├── flood/          # Flood risk (HAND + JRC)
├── bushfire/       # Bushfire risk (vegetation + slope + fire history)
├── common/         # Shared: DuckDB helpers, Overture loaders, geo utils
└── api/            # FastAPI endpoints
```

## Data Dependencies

All data is free and open-licensed:

- **Overture Maps** (CDLA Permissive) — roads, buildings, POI
- **Copernicus DEM GLO-30** (free) — elevation, slope
- **JRC Global Surface Water** (free) — 38-year flood history
- **Global Solar Atlas** (CC BY 4.0) — solar irradiance 250m
- **Sentinel-2** (free) — vegetation index 10m
- **NASA FIRMS** (free) — historical fire points
- **OSM** (ODbL) — amenities, land use

## License

MIT
