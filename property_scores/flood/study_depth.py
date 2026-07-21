"""reg-09: address-level flood DEPTH (metres) sampled from council hydraulic
study depth grids.

The flood score's overlays/hazard say *whether* and *how dangerous*; this says
*how deep* — the actual modelled water depth at the property for a design event,
the field NFID licenses to insurers. Source rasters are councils' own processed
hydraulic results (e.g. Central Coast Northern Lakes FRMS&P, CC BY 4.0), reprojected
to EPSG:4326 depth-in-metres COGs.

Design: a small registry of {bbox -> depth COG + provenance}; sample the first COG
whose bbox contains the point. Dormant when no COG is configured/reachable, so the
score is unchanged until depth grids are deployed (same safe-code / gated-data split
as the graded-hazard work). Production would carry many councils; this ships the one
CC-BY-open council proven in reg-09.
"""
from __future__ import annotations

import json
import logging
import os
from threading import Lock

log = logging.getLogger(__name__)

# Registry of council depth grids. `cog` is an env-overridable path so the same
# code runs against a local slice (dev) or the deployed COG store (prod). bbox is
# (xmin, ymin, xmax, ymax) in WGS84 — a cheap pre-filter before opening the raster.
#
# Entries come from two places, merged: this built-in (the reg-09 reference council)
# plus an optional manifest JSON written by the batch COG builder
# (reg09_localtest/build_depth_cogs.py) so "all councils" is data-driven, not code.
# Manifest path via STUDY_DEPTH_MANIFEST, else the deployed store's manifest.
_BUILTIN = [
    {
        "key": "central_coast_northern_lakes",
        "cog": os.environ.get(
            "STUDY_DEPTH_COG_CENTRAL_COAST",
            "/data/flood/study_depth/central_coast_northern_lakes_q100y_depth_4326.tif",
        ),
        "aep": "1% AEP",
        "source": "Central Coast Council — Northern Lakes FRMS&P (processed hydraulic results)",
        "licence": "CC BY 4.0",
        "bounds": (151.4549, -33.3022, 151.6015, -33.1826),
    },
]


def _load_registry():
    reg = list(_BUILTIN)
    manifest = os.environ.get("STUDY_DEPTH_MANIFEST",
                              "/data/flood/study_depth/manifest.json")
    try:
        if os.path.exists(manifest):
            entries = json.load(open(manifest))
            keys = {e["key"] for e in reg}
            for e in entries:
                if e.get("key") not in keys and e.get("cog") and e.get("bounds"):
                    reg.append(e)
    except Exception:
        log.warning("study depth manifest unreadable: %s", manifest, exc_info=True)
    return reg


_REGISTRY = _load_registry()

_NODATA_FLOOR = -1000.0   # reprojected NoData sentinel (~ -9999) sits well below this
_MIN_DEPTH_M = 0.05       # below this = effectively dry / model noise, not a flood hit

_ds_cache: dict[str, object] = {}
_lock = Lock()


def _open(path: str):
    """Cached rasterio handle for a depth COG, or None if unavailable."""
    with _lock:
        if path in _ds_cache:
            return _ds_cache[path]
        ds = None
        try:
            if os.path.exists(path):
                import rasterio
                ds = rasterio.open(path)
        except Exception:
            log.warning("study depth COG unavailable: %s", path, exc_info=True)
            ds = None
        _ds_cache[path] = ds
        return ds


def depth_at(lat: float, lng: float) -> dict | None:
    """Modelled flood depth (m) at a point, or None if outside every study grid
    / dry. Returns {depth_m, aep, source, licence}."""
    for entry in _REGISTRY:
        xmin, ymin, xmax, ymax = entry["bounds"]
        if not (xmin <= lng <= xmax and ymin <= lat <= ymax):
            continue
        ds = _open(entry["cog"])
        if ds is None:
            continue
        try:
            # GDAL dataset handles are NOT thread-safe: concurrent sample() on a
            # shared handle can crash under API load. Serialise reads — a point
            # sample is sub-ms, so the lock costs nothing in practice.
            with _lock:
                val = float(next(ds.sample([(lng, lat)]))[0])
        except (StopIteration, ValueError, Exception):
            continue
        if val < _NODATA_FLOOR or val < _MIN_DEPTH_M:
            continue  # NoData / dry / below the study extent
        return {
            "depth_m": round(val, 2),
            "aep": entry["aep"],
            "source": entry["source"],
            "licence": entry["licence"],
        }
    return None
