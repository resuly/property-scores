"""
Building screening attenuation for noise propagation.

Uses Overture Buildings (footprint + height) to detect if buildings block
the line-of-sight between a noise source and receiver. Applies Maekawa
barrier attenuation formula when screening is detected.

Two-phase approach: fetch all buildings in radius once (single parquet scan),
then compute attenuation per source-receiver pair in Python.
"""

import math

import numpy as np

from property_scores.common.config import data_path

BUILDINGS_FILE = "overture_buildings.parquet"
DEFAULT_BUILDING_HEIGHT = 6.0  # 2-storey house
RECEIVER_HEIGHT = 1.5  # ear height
SOURCE_HEIGHT_ROAD = 0.5  # tire noise height
SOURCE_HEIGHT_RAIL = 1.0  # rail noise height
SOUND_WAVELENGTH = 0.34  # ~1 kHz (dominant traffic noise frequency)
MAX_SINGLE_BARRIER_DB = 20.0  # physical limit for single thin barrier
MAX_TOTAL_BARRIER_DB = 25.0  # practical limit for multiple barriers


def buildings_in_radius(db, lat: float, lng: float,
                        radius_m: int) -> list[tuple[float, float, float]]:
    """Fetch all building centroids and heights within radius (single query).

    Returns list of (height, centroid_lng, centroid_lat).
    """
    buildings_path = data_path(BUILDINGS_FILE)
    if not buildings_path.exists():
        return []

    delta = radius_m / 111_000 * 1.5

    sql = f"""
        SELECT COALESCE(height, {DEFAULT_BUILDING_HEIGHT}) as h,
               ST_X(ST_Centroid(geometry)) as clng,
               ST_Y(ST_Centroid(geometry)) as clat
        FROM read_parquet('{buildings_path}')
        WHERE bbox.xmin < {lng + delta} AND bbox.xmax > {lng - delta}
          AND bbox.ymin < {lat + delta} AND bbox.ymax > {lat - delta}
    """
    try:
        return db.sql(sql).fetchall()
    except Exception:
        return []


def buildings_to_arrays(buildings: list[tuple[float, float, float]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not buildings:
        return np.empty(0), np.empty(0), np.empty(0)
    arr = np.array(buildings, dtype=np.float64)
    return arr[:, 0], arr[:, 1], arr[:, 2]


def barrier_attenuation(buildings: list[tuple[float, float, float]],
                        source_lng: float, source_lat: float,
                        receiver_lng: float, receiver_lat: float,
                        source_distance_m: float,
                        source_height: float = SOURCE_HEIGHT_ROAD,
                        *, _arrays=None) -> float:
    if source_distance_m < 20:
        return 0.0
    if _arrays is not None:
        heights, blng, blat = _arrays
    else:
        if not buildings:
            return 0.0
        heights, blng, blat = buildings_to_arrays(buildings)
    if len(heights) == 0:
        return 0.0
    return _barrier_np(heights, blng, blat,
                       source_lng, source_lat,
                       receiver_lng, receiver_lat,
                       source_distance_m, source_height)


def _barrier_np(heights, blng, blat,
                source_lng, source_lat,
                receiver_lng, receiver_lat,
                source_distance_m, source_height):
    m_per_deg = 111_320 * math.cos(math.radians((source_lat + receiver_lat) / 2))

    dx = (receiver_lng - source_lng) * m_per_deg
    dy = (receiver_lat - source_lat) * 111_320
    path_len = math.sqrt(dx * dx + dy * dy)
    if path_len < 1:
        return 0.0

    nx, ny = dx / path_len, dy / path_len

    bx = (blng - source_lng) * m_per_deg
    by = (blat - source_lat) * 111_320

    along = bx * nx + by * ny
    perp = np.abs(-bx * ny + by * nx)

    mask = (along > 5) & (along < source_distance_m - 5) & (perp < 30)
    if not np.any(mask):
        return 0.0

    a = along[mask]
    h = heights[mask]

    dist_to_rcv = source_distance_m - a
    over_src = np.sqrt(a ** 2 + (h - source_height) ** 2)
    over_rcv = np.sqrt(dist_to_rcv ** 2 + (h - RECEIVER_HEIGHT) ** 2)
    detour = over_src + over_rcv - source_distance_m

    pos_mask = detour > 0
    if not np.any(pos_mask):
        return 0.0

    a = a[pos_mask]
    detour = detour[pos_mask]

    fresnel_n = 2 * detour / SOUND_WAVELENGTH
    atten = np.minimum(10 * np.log10(3 + 20 * fresnel_n ** 2), MAX_SINGLE_BARRIER_DB)

    order = np.argsort(-atten)
    a = a[order]
    atten = atten[order]

    zones_used: set[int] = set()
    total = 0.0
    for i in range(len(atten)):
        zone = int(a[i] / 20)
        if zone in zones_used:
            continue
        zones_used.add(zone)
        total += float(atten[i]) if len(zones_used) == 1 else float(atten[i]) * 0.4
        if total >= MAX_TOTAL_BARRIER_DB:
            return MAX_TOTAL_BARRIER_DB

    return total
