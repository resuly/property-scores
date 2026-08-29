# Neighbourhood Heat

## Product boundary

This is long-term summer land-surface-temperature context, not parcel
temperature, live weather, heatwave forecasting, indoor comfort or an energy
model.

Two resolutions must always remain separate:

- NASA MOD11A2 day/night land-surface temperature: approximately 1 km
  (`temperature_native_grid_step_m=926.625`).
- ESA WorldCover canopy, built and water context: 10 m.

The local 10 m factors can explain why neighbouring streets differ, but they do
not turn the 1 km thermal observation into a 10 m temperature measurement.

## Current model

The score combines:

1. a local summer-median MOD11A2 day/night mosaic;
2. point versus surrounding-pixel UHI delta where the comparison is valid;
3. night heat retention;
4. Overture-derived building density; and
5. ESA WorldCover green cover.

If the address's MODIS pixel is empty, the nearest valid land-pixel ring inside
2 km may be used. The response then reports the offset and pixel count, and
withholds the UHI comparison. A true data gap returns `Data unavailable`; the
former Open-Meteo/ERA5 fallback was removed because its free tier is not
licensed for this commercial service.

## Vintage contract

`scripts/download_modis_lst.py` now defaults to the three most recent completed
southern-hemisphere summers. Every refresh builds new day/night tiles, VRTs and
the manifest inside one versioned staging directory. It requires the exact same
complete tile IDs on both sides, then atomically switches the single
`data/global/modis_lst_current` symlink. There is no unfingerprinted
`--skip-existing` path, so old tiles cannot be relabelled with a new vintage.

The API never infers vintage from file modification time. An older installation
without the sidecar returns `temperature_vintage.status=unverified` until it is
rebuilt. A score call resolves the symlink to one release directory before it
opens either VRT, so day, night and vintage come from the same generation.

## Output additions

- `product=neighbourhood_heat`
- `assessment_level=neighbourhood_context`
- `temperature_resolution_m` and `land_cover_resolution_m`
- `temperature_vintage`
- `day_night_cooling_c`
- response-level source, licence and attribution rows

The legacy score, labels, MODIS fields, borrowed-pixel disclosure and local
factor fields remain compatible.

## Future evidence gate

Landsat Collection 2 surface temperature can add a roughly 100 m native thermal
signal, but it is a new source and model path with cloud, emissivity, temporal
sampling and validation work. It should start only after a named portfolio,
council or property-report evaluator demonstrates that the current 1 km signal
cannot answer a repeated decision.
