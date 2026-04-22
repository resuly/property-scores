"""
Building screening attenuation for noise propagation.

Uses Overture Buildings (footprint + height) to detect if buildings block
the line-of-sight between a noise source and receiver. Applies Maekawa
barrier attenuation formula when screening is detected.

Two-phase approach: fetch all buildings in radius once (single parquet scan),
then compute attenuation per source-receiver pair in Python.
"""

import math

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


def barrier_attenuation(buildings: list[tuple[float, float, float]],
                        source_lng: float, source_lat: float,
                        receiver_lng: float, receiver_lat: float,
                        source_distance_m: float,
                        source_height: float = SOURCE_HEIGHT_ROAD) -> float:
    """Calculate barrier attenuation using pre-fetched buildings.

    Args:
        buildings: list of (height, centroid_lng, centroid_lat) from buildings_in_radius()

    Returns attenuation in dB (positive = noise reduction).
    """
    if source_distance_m < 20 or not buildings:
        return 0.0

    m_per_deg = 111_320 * math.cos(math.radians((source_lat + receiver_lat) / 2))

    dx = (receiver_lng - source_lng) * m_per_deg
    dy = (receiver_lat - source_lat) * 111_320
    path_len = math.sqrt(dx * dx + dy * dy)
    if path_len < 1:
        return 0.0

    nx, ny = dx / path_len, dy / path_len

    barriers: list[tuple[float, float]] = []  # (along_position, attenuation)
    for bldg_height, clng, clat in buildings:
        bx = (clng - source_lng) * m_per_deg
        by = (clat - source_lat) * 111_320

        along = bx * nx + by * ny
        if along < 5 or along > source_distance_m - 5:
            continue

        perp = abs(-bx * ny + by * nx)
        if perp > 30:
            continue

        dist_to_rcv = source_distance_m - along
        over_src = math.sqrt(along ** 2 + (bldg_height - source_height) ** 2)
        over_rcv = math.sqrt(dist_to_rcv ** 2 + (bldg_height - RECEIVER_HEIGHT) ** 2)
        detour = over_src + over_rcv - source_distance_m

        if detour <= 0:
            continue

        fresnel_n = 2 * detour / SOUND_WAVELENGTH
        atten = min(10 * math.log10(3 + 20 * fresnel_n ** 2), MAX_SINGLE_BARRIER_DB)

        barriers.append((along, atten))

    if not barriers:
        return 0.0

    # Multiple barriers: keep best per 20m zone, sum top barriers (diminishing)
    barriers.sort(key=lambda x: x[1], reverse=True)
    zones_used: set[int] = set()
    total = 0.0
    for along, atten in barriers:
        zone = int(along / 20)
        if zone in zones_used:
            continue
        zones_used.add(zone)
        if not zones_used - {zone}:
            total += atten
        else:
            total += atten * 0.4  # diminishing return for additional barriers
        if total >= MAX_TOTAL_BARRIER_DB:
            return MAX_TOTAL_BARRIER_DB

    return total
