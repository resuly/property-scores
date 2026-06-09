"""Generic raster sampler for global feature layers (DEM, land cover, population).

Recipe-agnostic: give it any raster (local path or /vsicurl//vsis3/ COG) and a
lat/lng, it samples the value (reprojecting the point to the raster CRS). Also
computes window stats (mean / categorical fractions) in a metre radius.

Handles the local PROJ conflict (EclipseSUMO proj.db) by forcing rasterio's
bundled proj data. No pyproj (not installed) — uses rasterio.warp.transform.
"""
import os

# Force rasterio's bundled PROJ before importing rasterio's warp
import rasterio  # noqa: E402
_proj = os.path.join(os.path.dirname(rasterio.__file__), "proj_data")
if os.path.isdir(_proj):
    os.environ.setdefault("PROJ_LIB", _proj)
    os.environ.setdefault("PROJ_DATA", _proj)

import threading  # noqa: E402

import numpy as np  # noqa: E402
from rasterio.warp import transform as _rio_transform  # noqa: E402

# Per-thread open-handle cache. rasterio/GDAL DatasetReader objects are NOT
# thread-safe for concurrent read()/sample()/index(); a shared handle read by
# many threads (FastAPI threadpool + per-request ThreadPoolExecutor) corrupts
# GDAL band-cache/cursor state. Each thread keeps its own handle dict, so reads
# never race and the check-then-open is confined to a single thread (no leak).
_LOCAL = threading.local()


def _src(path):
    cache = getattr(_LOCAL, "open", None)
    if cache is None:
        cache = {}
        _LOCAL.open = cache
    if path not in cache:
        if not (path.startswith("/vsi") or os.path.exists(path)):
            cache[path] = None
        else:
            try:
                cache[path] = rasterio.open(path)
            except Exception:
                cache[path] = None
    return cache[path]


def _to_raster_xy(src, lat, lng):
    if src.crs is None or src.crs.to_epsg() == 4326:
        return lng, lat
    xs, ys = _rio_transform("EPSG:4326", src.crs, [lng], [lat])
    return xs[0], ys[0]


def sample(path, lat, lng, default=np.nan):
    """Single value at a point."""
    src = _src(path)
    if src is None:
        return default
    x, y = _to_raster_xy(src, lat, lng)
    try:
        v = next(src.sample([(x, y)]))[0]
    except (StopIteration, IndexError):
        return default
    if src.nodata is not None and v == src.nodata:
        return default
    return float(v)


def window_stats(path, lat, lng, radius_m, categorical=False, classes=None):
    """Window stats in a metre radius around the point.

    Continuous: returns {'mean','max'}. Categorical: returns {'frac_<cls>': ...}
    for the requested class codes.
    """
    src = _src(path)
    if src is None:
        return {}
    x, y = _to_raster_xy(src, lat, lng)
    # pixel size in raster units; assume projected metres or degrees
    px = abs(src.transform.a)
    if src.crs and src.crs.to_epsg() == 4326:
        rad = radius_m / (111_320.0)  # degrees
    else:
        rad = radius_m
    half = max(int(rad / px), 1)
    try:
        row, col = src.index(x, y)
    except Exception:
        return {}
    win = ((max(row - half, 0), row + half + 1), (max(col - half, 0), col + half + 1))
    try:
        arr = src.read(1, window=win)
    except Exception:
        return {}
    if src.nodata is not None:
        arr = arr[arr != src.nodata]
    if arr.size == 0:
        return {}
    if categorical:
        out = {}
        tot = arr.size
        for c in (classes or np.unique(arr)):
            out[f"frac_{int(c)}"] = float(np.mean(arr == c))
        return out
    return {"mean": float(np.mean(arr)), "max": float(np.max(arr))}
