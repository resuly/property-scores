Real bytes cut from the production transfer-model rasters on 2026-11-06
(`data/global/lc.vrt` ESA WorldCover 2021 v200, `data/global/dem.vrt`), a
+-0.012 deg window around Tuart Hill WA (-31.89132661, 115.8351207). Read with
rasterio and written as GeoTIFF with the source CRS, transform and dtype
unchanged. `expected_raster_feats.json` is what the production code computed
at that point from the full VRTs on the same day: the crop reproduces it.

ESA WorldCover: (c) ESA WorldCover project / Contains modified Copernicus
Sentinel data (2021), CC BY 4.0.
