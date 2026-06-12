"""ESA WorldCover 10m land-cover helpers (shared by view-quality / heat-island).

Wraps the generic raster sampler (loaded by file path so we don't pull the heavy
noise package __init__) over the local WorldCover mosaic ``data/global/lc.vrt``.
The sampler's open handles are thread-local, so concurrent reads are safe.

Returns None / {} when the mosaic is absent or the point is outside tile
coverage, so callers degrade gracefully to their existing POI proxy.
"""

import importlib.util as _ilu
import os as _os
import threading as _threading

from property_scores.common.config import data_path

LC_VRT = str(data_path("global/lc.vrt"))

# ESA WorldCover v2 class codes
TREE, SHRUB, GRASS, CROP, BUILT, BARE = 10, 20, 30, 40, 50, 60
SNOW, WATER, WETLAND, MANGROVE, MOSS = 70, 80, 90, 95, 100
_ALL = (TREE, SHRUB, GRASS, CROP, BUILT, BARE, SNOW, WATER, WETLAND, MANGROVE, MOSS)
_VEGETATED = (TREE, SHRUB, GRASS, CROP, WETLAND, MANGROVE)  # flammable/green vegetation
_WOODY = (TREE, SHRUB)  # canopy = what reads as "green outlook" / shade

_RS = None
_RS_LOCK = _threading.Lock()


def _sampler():
    global _RS
    if _RS is None:
        with _RS_LOCK:
            if _RS is None:
                path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)),
                                     "noise", "raster_sample.py")
                spec = _ilu.spec_from_file_location("_pscore_raster_sample", path)
                mod = _ilu.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _RS = mod
    return _RS


def sampler():
    """The shared generic raster-sample module (single process-wide instance, so
    bushfire / view-quality / heat-island reuse one per-thread handle cache)."""
    return _sampler()


def available() -> bool:
    return _os.path.exists(LC_VRT)


def fractions(lat: float, lng: float, radius_m: int = 300) -> dict:
    """Area fraction per WorldCover class in a radius. {} if unavailable / no data."""
    if not available():
        return {}
    try:
        fr = _sampler().window_stats(LC_VRT, lat, lng, radius_m=radius_m,
                                     categorical=True, classes=list(_ALL),
                                     cos_correct=True)
    except Exception:
        return {}
    if not fr:
        return {}
    return {c: fr.get(f"frac_{c}", 0.0) for c in _ALL}


def green_fraction(lat: float, lng: float, radius_m: int = 300) -> float | None:
    """Vegetated (tree+shrub+grass+crop+wetland) area fraction, or None."""
    fr = fractions(lat, lng, radius_m)
    if not fr:
        return None
    return round(sum(fr[c] for c in _VEGETATED), 3)


def canopy_fraction(lat: float, lng: float, radius_m: int = 300) -> float | None:
    """Woody canopy (tree+shrub) area fraction, or None. = green outlook / shade."""
    fr = fractions(lat, lng, radius_m)
    if not fr:
        return None
    return round(fr[TREE] + fr[SHRUB], 3)
