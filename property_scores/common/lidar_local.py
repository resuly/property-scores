"""Local national 5 m LiDAR bare-earth DEM (data/global/lidar/au_lidar_5m.vrt).

The baked, offline replacement for the five live state LiDAR API providers in
`flood/lidar.py`. One VRT over per-UTM-zone Int16-decimetre COGs (GA "5 Metre DEM
of Australia derived from LiDAR", ~245,000 km2 of populated coast + Murray-Darling
floodplain + towns). Sampled through the shared per-thread raster sampler, so the
handle opens once per thread alongside DEM / land-cover.

Values are decimetres (x10 of metres) so we divide by 10. Coverage is gated on
the real nodata (-32768): outside the LiDAR footprint the sampler returns NaN and
we return None, so the flood HAND read falls through to DEM-H 30 m — exactly like
terrain.py gates the DEM on tile coverage. Confidence for a covered read = high.

See docs/national-lidar-bake-handoff.md; scripts/bake_lidar_cog.py builds the VRT.
"""
import os as _os

from property_scores.common.config import data_path
from property_scores.noise import raster_sample as _rs

LIDAR_VRT = str(data_path("global/lidar/au_lidar_5m.vrt"))


def available() -> bool:
    return _os.path.exists(LIDAR_VRT)


def elevation(lat: float, lng: float) -> float | None:
    """Bare-earth elevation in metres from the local 5 m LiDAR VRT, or None
    outside the baked footprint (nodata) / on any read failure."""
    if not available():
        return None
    try:
        v = _rs.sample(LIDAR_VRT, lat, lng)  # decimetres; NaN if nodata
    except Exception:
        return None
    if v is None or v != v:  # None or NaN (nodata / uncovered)
        return None
    return float(v) / 10.0
