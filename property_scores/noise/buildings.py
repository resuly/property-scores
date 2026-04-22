"""
Building screening attenuation for noise propagation.

Uses Overture Buildings (footprint + height) to detect if buildings block
the line-of-sight between a noise source and receiver. Applies Maekawa
barrier attenuation formula when screening is detected.

Simplified 2D approach: cast ray from source to receiver, find intersecting
buildings, use building height to calculate path-length difference.
"""

import math

from property_scores.common.config import data_path

BUILDINGS_FILE = "overture_buildings.parquet"
DEFAULT_BUILDING_HEIGHT = 6.0  # 2-storey house
RECEIVER_HEIGHT = 1.5  # ear height
SOURCE_HEIGHT_ROAD = 0.5  # tire noise height
SOURCE_HEIGHT_RAIL = 1.0  # rail noise height
SOUND_WAVELENGTH = 0.34  # ~1 kHz (dominant traffic noise frequency)
MAX_BARRIER_ATTENUATION = 20.0  # physical limit for single thin barrier


def _buildings_between(db, src_lng: float, src_lat: float,
                       rcv_lng: float, rcv_lat: float,
                       buffer_m: float = 30.0) -> list[tuple[float, float]]:
    """Find buildings intersecting the line from source to receiver.

    Returns list of (distance_from_source_m, building_height).
    Uses a buffered bounding box query + intersection test.
    """
    buildings_path = data_path(BUILDINGS_FILE)
    if not buildings_path.exists():
        return []

    mid_lat = (src_lat + rcv_lat) / 2
    m_per_deg = 111_320 * math.cos(math.radians(mid_lat))
    buf_deg = buffer_m / m_per_deg

    min_lng = min(src_lng, rcv_lng) - buf_deg
    max_lng = max(src_lng, rcv_lng) + buf_deg
    min_lat = min(src_lat, rcv_lat) - buf_deg
    max_lat = max(src_lat, rcv_lat) + buf_deg

    table = f"read_parquet('{buildings_path}')"

    sql = f"""
        SELECT COALESCE(height, {DEFAULT_BUILDING_HEIGHT}) as h,
               ST_Distance(
                   ST_Centroid(geometry),
                   ST_Point({src_lng}, {src_lat})
               ) * {m_per_deg} as dist_from_src
        FROM {table}
        WHERE bbox.xmin < {max_lng} AND bbox.xmax > {min_lng}
          AND bbox.ymin < {max_lat} AND bbox.ymax > {min_lat}
          AND ST_Intersects(
              geometry,
              ST_Buffer(
                  ST_MakeLine(
                      ST_Point({src_lng}, {src_lat}),
                      ST_Point({rcv_lng}, {rcv_lat})
                  ),
                  {buf_deg * 0.3}
              )
          )
        ORDER BY dist_from_src
        LIMIT 5
    """
    try:
        return db.sql(sql).fetchall()
    except Exception:
        return []


def barrier_attenuation(db, source_lng: float, source_lat: float,
                        receiver_lng: float, receiver_lat: float,
                        source_distance_m: float,
                        source_height: float = SOURCE_HEIGHT_ROAD) -> float:
    """Calculate barrier attenuation from buildings between source and receiver.

    Returns attenuation in dB (positive = noise reduction).
    """
    if source_distance_m < 20:
        return 0.0

    buildings = _buildings_between(db, source_lng, source_lat,
                                   receiver_lng, receiver_lat)
    if not buildings:
        return 0.0

    max_atten = 0.0
    for bldg_height, dist_from_src in buildings:
        if dist_from_src < 5 or dist_from_src > source_distance_m - 5:
            continue

        dist_to_rcv = source_distance_m - dist_from_src
        direct_path = source_distance_m

        # Path over building top
        over_src = math.sqrt(dist_from_src**2 + (bldg_height - source_height)**2)
        over_rcv = math.sqrt(dist_to_rcv**2 + (bldg_height - RECEIVER_HEIGHT)**2)
        detour = over_src + over_rcv - direct_path

        if detour <= 0:
            continue

        # Maekawa formula: Abar = 10 * log10(3 + 20*N^2)
        # N = 2 * delta / lambda (Fresnel number)
        fresnel_n = 2 * detour / SOUND_WAVELENGTH
        atten = 10 * math.log10(3 + 20 * fresnel_n**2)
        atten = min(atten, MAX_BARRIER_ATTENUATION)

        if atten > max_atten:
            max_atten = atten

    return max_atten


def screening_for_point(db, lat: float, lng: float,
                        road_segments: list[tuple[float, float, float]]) -> dict[int, float]:
    """Calculate screening attenuation for each road segment from a receiver point.

    Args:
        road_segments: list of (source_lng, source_lat, distance_m)

    Returns:
        dict mapping segment index to attenuation in dB.
    """
    result = {}
    for i, (src_lng, src_lat, dist_m) in enumerate(road_segments):
        if dist_m < 30:
            result[i] = 0.0
            continue
        atten = barrier_attenuation(db, src_lng, src_lat, lng, lat, dist_m)
        result[i] = atten
    return result
