"""Elevation contour lines from the baked GA national 5 m LiDAR DEM.

Replaces the five live state-government ArcGIS contour services the licensed
property API used to relay (VIC/NSW/QLD/TAS/WA): the NSW terms are
self-contradictory on licence version, forbid automated harvesting, cap usage
and carry an indemnity, and the service itself is flagged RETIRING, while the
TAS/WA attribution requirements were not being met. The revised customer
Schedule A registers ONE contour source -- Geoscience Australia's "5 Metre DEM
of Australia derived from LiDAR" (DOI 10.26186/89644, CC BY 4.0) -- so the
contours must come from that grid and nothing else.

The grid is the same baked VRT `common/lidar_local.py` samples for flood HAND
(data/global/lidar/au_lidar_5m.vrt): EPSG:4326 at ~5 m (0.0000449 deg), Int16
DECIMETRES (divide by 10 for metres), nodata -32768. Coverage is ~245,000 km2
of populated coast, the Murray-Darling floodplain and towns -- NOT national.
Outside the footprint this module reports no coverage; it must never fall back
to the 30 m DEM-H or any other source, because the customer contract registers
only the GA 5 m grid for this layer.

Contours are generated here by marching squares over a window read of the VRT.
skimage/matplotlib are deliberately not used: neither is installed in this
service's venv and neither is worth adding as a dependency for one textbook
algorithm. Cells touching nodata are skipped outright, so lines terminate at
the footprint edge rather than interpolating into nodata (no invented lines
across coverage gaps).

Vertical accuracy of the source is 0.30 m (95%), so the finest interval this
module will draw is 1 m; requests below that are raised and labelled. The
default spacing is 5 m (matching the 5 m grid), widened automatically over
steep windows so a response stays readable.
"""

from __future__ import annotations

import math
import os

import numpy as np

from property_scores.common.lidar_local import LIDAR_VRT
from property_scores.noise import raster_sample as _rs

NODATA = -32768
DECIMETRES_PER_METRE = 10.0
GRID_M = 5

# Source vertical accuracy is 0.30 m (95%): a sub-metre interval would draw
# lines closer together than the data can distinguish.
INTERVAL_FLOOR_M = 1.0
INTERVAL_DEFAULT_M = 5.0
NICE_INTERVALS = (1, 2, 5, 10, 20, 25, 50, 100, 200)
TARGET_LEVELS = 20
MAX_LEVELS = 200

RADIUS_MIN_M = 200
RADIUS_MAX_M = 2000

# Light Douglas-Peucker generalisation, ~5 m in degrees: one vertex per grid
# cell is denser than any property-window rendering can show.
SIMPLIFY_DEG = 0.00005

# case index = TL*8 | TR*4 | BR*2 | BL*1 ("above the level" per corner).
# Values are pairs of cell edges the contour crosses: T top, B bottom,
# L left, R right. Cases 5 and 10 are saddles, resolved on the cell centre.
_CASE_SEGMENTS = {
    1: (("L", "B"),),
    2: (("B", "R"),),
    3: (("L", "R"),),
    4: (("T", "R"),),
    6: (("T", "B"),),
    7: (("T", "L"),),
    8: (("T", "L"),),
    9: (("T", "B"),),
    11: (("T", "R"),),
    12: (("L", "R"),),
    13: (("B", "R"),),
    14: (("L", "B"),),
}


def lidar_available(path: str | None = None) -> bool:
    return os.path.exists(path or LIDAR_VRT)


def _deg_per_m(lat: float) -> tuple[float, float]:
    return (1.0 / 111_320.0,
            1.0 / (111_320.0 * max(math.cos(math.radians(lat)), 0.1)))


def _read_window(path: str, lat: float, lng: float, radius_m: int):
    """Read the DEM window around the point.

    Returns (z_metres, node_lngs, node_lats) or None when the raster cannot be
    read or the window falls entirely off the raster. z is float64 with NaN at
    nodata; node arrays give the lng of every column and lat of every row
    (pixel centres).
    """
    import rasterio.windows

    src = _rs._src(path)
    if src is None:
        return None
    dlat_per_m, dlng_per_m = _deg_per_m(lat)
    west = lng - radius_m * dlng_per_m
    east = lng + radius_m * dlng_per_m
    south = lat - radius_m * dlat_per_m
    north = lat + radius_m * dlat_per_m
    try:
        win = rasterio.windows.from_bounds(west, south, east, north,
                                           transform=src.transform)
        win = win.round_offsets().round_lengths()
        full = rasterio.windows.Window(0, 0, src.width, src.height)
        win = win.intersection(full)
        if win.width < 2 or win.height < 2:
            return None
        raw = src.read(1, window=win)
    except Exception:
        return None
    t = src.window_transform(win)
    z = raw.astype("float64")
    nod = src.nodata if src.nodata is not None else NODATA
    z[raw == nod] = np.nan
    z /= DECIMETRES_PER_METRE
    ncols = z.shape[1]
    nrows = z.shape[0]
    # Pixel centres. The VRT is EPSG:4326 with no rotation terms.
    lngs = t.c + (np.arange(ncols) + 0.5) * t.a
    lats = t.f + (np.arange(nrows) + 0.5) * t.e
    return z, lngs, lats


def _edge_point(zl: np.ndarray, level: float, edge) -> tuple[float, float]:
    """(row, col) float coordinates of the crossing on a grid edge."""
    kind, r, c = edge
    v1 = zl[r, c]
    v2 = zl[r, c + 1] if kind == "h" else zl[r + 1, c]
    t = (level - v1) / (v2 - v1)
    t = min(max(t, 0.0), 1.0)
    if kind == "h":
        return (float(r), c + t)
    return (r + t, float(c))


def _cell_edges(r: int, c: int) -> dict:
    return {"T": ("h", r, c), "B": ("h", r + 1, c),
            "L": ("v", r, c), "R": ("v", r, c + 1)}


def _segments_for_level(z: np.ndarray, level: float) -> list[tuple]:
    """Marching-squares segments for one level, as pairs of edge ids.

    Cells with any nodata (NaN) corner are skipped entirely, so contour lines
    stop at the coverage boundary instead of being interpolated across it.
    """
    zl = np.where(z == level, level + 1e-6, z)
    with np.errstate(invalid="ignore"):
        above = zl > level
    finite = np.isfinite(zl)
    tl = above[:-1, :-1]
    tr = above[:-1, 1:]
    br = above[1:, 1:]
    bl = above[1:, :-1]
    valid = finite[:-1, :-1] & finite[:-1, 1:] & finite[1:, 1:] & finite[1:, :-1]
    case = (tl.astype(np.uint8) << 3 | tr.astype(np.uint8) << 2
            | br.astype(np.uint8) << 1 | bl.astype(np.uint8))
    interesting = valid & (case != 0) & (case != 15)
    segments: list[tuple] = []
    for r, c in np.argwhere(interesting):
        r = int(r)
        c = int(c)
        cs = int(case[r, c])
        edges = _cell_edges(r, c)
        if cs in (5, 10):
            centre_above = (zl[r, c] + zl[r, c + 1]
                            + zl[r + 1, c] + zl[r + 1, c + 1]) / 4.0 > level
            if cs == 5:      # TR and BL above
                pairs = (("T", "L"), ("B", "R")) if centre_above \
                    else (("T", "R"), ("L", "B"))
            else:            # TL and BR above
                pairs = (("T", "R"), ("L", "B")) if centre_above \
                    else (("T", "L"), ("B", "R"))
        else:
            pairs = _CASE_SEGMENTS[cs]
        for a, b in pairs:
            segments.append((edges[a], edges[b]))
    return segments


def _chain(segments: list[tuple]) -> list[list]:
    """Join segments sharing an edge id into paths of edge ids.

    Every crossing point sits on exactly one grid edge and belongs to at most
    two cells, so degree is at most 2 and the result is simple open paths and
    closed rings. Closed rings repeat their first edge id at the end.
    """
    adj: dict = {}
    for a, b in segments:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)

    visited: set = set()

    def seg_key(a, b):
        return (a, b) if a <= b else (b, a)

    paths: list[list] = []
    # Open paths first, from every degree-1 endpoint.
    for start, nbrs in adj.items():
        if len(nbrs) != 1:
            continue
        if all(seg_key(start, n) in visited for n in nbrs):
            continue
        path = [start]
        prev, cur = None, start
        while True:
            nxt = next((n for n in adj[cur]
                        if seg_key(cur, n) not in visited), None)
            if nxt is None:
                break
            visited.add(seg_key(cur, nxt))
            path.append(nxt)
            prev, cur = cur, nxt
        if len(path) > 1:
            paths.append(path)
    # What remains is closed rings.
    for start, nbrs in adj.items():
        nxt = next((n for n in nbrs if seg_key(start, n) not in visited), None)
        if nxt is None:
            continue
        path = [start]
        cur = start
        while True:
            nxt = next((n for n in adj[cur]
                        if seg_key(cur, n) not in visited), None)
            if nxt is None:
                break
            visited.add(seg_key(cur, nxt))
            path.append(nxt)
            cur = nxt
        if len(path) > 2 and path[-1] != path[0] and start in adj[path[-1]]:
            # seg back to start already consumed above when the walk closed.
            path.append(path[0])
        if len(path) > 1:
            paths.append(path)
    return paths


def _pick_interval(relief_m: float) -> float:
    """Auto spacing: ~TARGET_LEVELS bands, never finer than the 5 m default."""
    want = relief_m / TARGET_LEVELS if relief_m > 0 else INTERVAL_DEFAULT_M
    for nice in NICE_INTERVALS:
        if nice >= want:
            return float(max(nice, INTERVAL_DEFAULT_M))
    return float(NICE_INTERVALS[-1])


def _levels_between(lo: float, hi: float, step: float) -> list[float]:
    start = math.ceil(lo / step) * step
    levels = []
    v = start
    while v <= hi and len(levels) < MAX_LEVELS:
        levels.append(round(v, 3))
        v += step
    return levels


def _simplify(coords: list) -> list:
    """Douglas-Peucker via shapely where available; identity otherwise."""
    if len(coords) < 3:
        return coords
    try:
        from shapely.geometry import LineString
    except ImportError:
        return coords
    try:
        simplified = LineString(coords).simplify(SIMPLIFY_DEG,
                                                 preserve_topology=False)
        out = list(simplified.coords)
    except Exception:
        return coords
    return out if len(out) >= 2 else coords


def contours(lat: float, lng: float, radius_m: int = 1500,
             interval_m: float | None = None,
             path: str | None = None) -> dict | None:
    """Contour LineStrings around a point from the GA 5 m LiDAR DEM.

    Returns None when the window holds no LiDAR coverage at all (the caller
    turns that into a 404: outside the ~245,000 km2 baked footprint there IS
    no registered source, and substituting another DEM is contractually off
    the table). Otherwise returns a dict with GeoJSON features, the interval
    actually used and coverage honesty fields.
    """
    path = path or LIDAR_VRT
    radius_m = int(max(RADIUS_MIN_M, min(radius_m, RADIUS_MAX_M)))
    got = _read_window(path, lat, lng, radius_m)
    if got is None:
        return None
    z, lngs, lats = got
    finite = np.isfinite(z)
    total = z.size
    covered = int(finite.sum())
    if not covered:
        return None
    covered_fraction = covered / total

    lo = float(np.nanmin(z))
    hi = float(np.nanmax(z))

    if interval_m:
        step = float(max(float(interval_m), INTERVAL_FLOOR_M))
        source = ("raised_to_floor" if float(interval_m) < INTERVAL_FLOOR_M
                  else "requested")
    else:
        step = _pick_interval(hi - lo)
        source = "auto"

    features = []
    levels_present = []
    for level in _levels_between(lo, hi, step):
        segs = _segments_for_level(z, level)
        if not segs:
            continue
        zl = np.where(z == level, level + 1e-6, z)
        drew = False
        for edge_path in _chain(segs):
            pts = [_edge_point(zl, level, e) for e in edge_path]
            coords = [(round(float(lngs[0] + (lngs[1] - lngs[0]) * col), 6),
                       round(float(lats[0] + (lats[1] - lats[0]) * row), 6))
                      for row, col in pts]
            coords = _simplify(coords)
            # Drop degenerate leftovers (a segment collapsed by rounding).
            if len({(x, y) for x, y in coords}) < 2:
                continue
            features.append({
                "type": "Feature",
                "properties": {"elevation_m": round(level, 2)},
                "geometry": {"type": "LineString",
                             "coordinates": [[x, y] for x, y in coords]},
            })
            drew = True
        if drew:
            levels_present.append(round(level, 2))

    bbox = [round(float(lngs[0]), 6), round(float(min(lats[0], lats[-1])), 6),
            round(float(lngs[-1]), 6), round(float(max(lats[0], lats[-1])), 6)]
    return {
        "bbox": bbox,
        "radius_m": radius_m,
        "grid_m": GRID_M,
        "vertical_accuracy_m": 0.30,
        "interval_m": step,
        "interval_source": source,
        "interval_floor_m": INTERVAL_FLOOR_M,
        "generalised_deg": SIMPLIFY_DEG,
        "coverage": "full" if covered == total else "partial",
        "covered_fraction": round(covered_fraction, 4),
        "elevation_min_m": round(lo, 2),
        "elevation_max_m": round(hi, 2),
        "levels": levels_present,
        "feature_count": len(features),
        "features": features,
    }
