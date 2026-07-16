"""Local elevation helper: 5 m LiDAR first, 30 m DEM fallback.

elevation() is the single entry point every score (bushfire slope, walkability
slope penalty, noise terrain screening, flood HAND fallback) samples through.
It prefers the baked national 5 m LiDAR bare-earth VRT (common.lidar_local,
~245,000 km2 of populated coverage) and falls back to the 30 m DEM
(data/global/dem.vrt) outside the LiDAR footprint. Near the footprint edge a
multi-point read can mix 5 m and 30 m samples; both are bare-earth AHD, so the
seam is within the DEM's own vertical error.

Reuses the single shared raster sampler (common.landcover.sampler) so the DEM
handle is opened once per thread alongside the WorldCover handle.

The DEM VRT has NO NoDataValue, so points outside the downloaded tiles read back
as a bogus 0.0 (not nodata). We therefore gate every read on real tile coverage,
derived from the 1x1 degree tile filenames, and return None outside coverage so
callers fall back to a remote elevation source instead of trusting a 0.0 gap.
"""

import math as _math
import os as _os
import re as _re

from property_scores.common.config import data_path
from property_scores.common.landcover import sampler as _sampler

DEM_VRT = str(data_path("global/dem.vrt"))
_DEM_DIR = str(data_path("global/dem"))
# Copernicus tile name, SW corner: ..._10_S38_00_E145_00_DEM.tif
_TILE_RE = _re.compile(r"_10_([NS])(\d+)_00_([EW])(\d+)_00_")
_covered_cells = None


def available() -> bool:
    return _os.path.exists(DEM_VRT)


def _coverage():
    """Set of (floor_lat, floor_lng) 1-degree cells that have a real DEM tile."""
    global _covered_cells
    if _covered_cells is None:
        cells = set()
        try:
            for fn in _os.listdir(_DEM_DIR):
                m = _TILE_RE.search(fn)
                if not m:
                    continue
                ns, la, ew, ln = m.groups()
                cells.add((int(la) * (1 if ns == "N" else -1),
                           int(ln) * (1 if ew == "E" else -1)))
        except Exception:
            pass
        _covered_cells = cells
    return _covered_cells


def covered(lat: float, lng: float) -> bool:
    return (_math.floor(lat), _math.floor(lng)) in _coverage()


def elevation(lat: float, lng: float) -> float | None:
    """Elevation in metres: 5 m LiDAR where baked, else 30 m DEM, else None."""
    try:
        from property_scores.common import lidar_local
        v = lidar_local.elevation(lat, lng)
        if v is not None:
            return v
    except Exception:
        pass
    return dem_elevation(lat, lng)


def dem_elevation(lat: float, lng: float) -> float | None:
    """Elevation in metres from the 30 m DEM only, or None outside tile coverage."""
    if not available() or not covered(lat, lng):
        return None
    try:
        v = _sampler().sample(DEM_VRT, lat, lng)
    except Exception:
        return None
    if v is None or v != v:  # None or NaN
        return None
    return float(v)
