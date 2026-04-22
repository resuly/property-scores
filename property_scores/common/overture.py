"""Overture Maps data loading helpers via DuckDB."""

import duckdb

from property_scores.common.config import data_path

import threading

ROADS_FILE = "overture_roads.parquet"
POIS_FILE = "overture_pois.parquet"
AADT_FILE = "vicroads_aadt_2019.parquet"
NFDH_FILE = "nfdh_aadt_national.parquet"
PTV_SHAPES_FILE = "ptv_rail_shapes.parquet"
PTV_FREQ_FILE = "ptv_rail_frequency.parquet"
AU_RAIL_SHAPES_FILE = "au_rail_shapes.parquet"
AU_RAIL_FREQ_FILE = "au_rail_frequency.parquet"

_install_lock = threading.Lock()
_installed = False


def get_db() -> duckdb.DuckDBPyConnection:
    global _installed
    db = duckdb.connect()
    with _install_lock:
        if not _installed:
            db.install_extension("spatial")
            _installed = True
    db.load_extension("spatial")
    return db


def _local_or_fail(filename: str) -> str:
    p = data_path(filename)
    if not p.exists():
        raise FileNotFoundError(
            f"Data file not found: {p}\n"
            f"Run: python -m property_scores.common.download --type roads"
        )
    return str(p)


def roads_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
               radius_m: int = 1000, *, source: str | None = None) -> list[tuple]:
    table = f"read_parquet('{source or _local_or_fail(ROADS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT class,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m,
               CASE WHEN speed_limits IS NOT NULL AND len(speed_limits) > 0
                    THEN speed_limits[1].max_speed.value
                    ELSE NULL END AS speed_kmh
        FROM {table}
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
          AND subtype = 'road'
    """
    return db.sql(sql).fetchall()


def rail_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 1000, *, source: str | None = None) -> list[tuple]:
    """Find tram/train segments within radius. Returns (class, dist_m)."""
    table = f"read_parquet('{source or _local_or_fail(ROADS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT class,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m
        FROM {table}
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
          AND subtype = 'rail'
    """
    return db.sql(sql).fetchall()


def aadt_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 500) -> list[tuple]:
    """Find VicRoads AADT segments within radius.

    Returns (aadt, hv_pct, road_name, dist_m, nearest_lng, nearest_lat).
    """
    aadt_path = data_path(AADT_FILE)
    if not aadt_path.exists():
        return []
    table = f"read_parquet('{aadt_path}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT aadt, hv_pct, road_name,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m,
               ST_X(ST_ClosestPoint(geometry, ST_Point({lng}, {lat}))) AS near_lng,
               ST_Y(ST_ClosestPoint(geometry, ST_Point({lng}, {lat}))) AS near_lat
        FROM {table}
        WHERE xmin BETWEEN {lng - delta} AND {lng + delta}
          AND ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
        ORDER BY dist_m
    """
    return db.sql(sql).fetchall()


def nfdh_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 1000) -> list[tuple]:
    """Find NFDH national traffic counter stations within radius.

    Aggregates directional/lane counts into total AADT per station.
    Returns (aadt, hv_pct, road_name, dist_m, station_lng, station_lat).
    """
    nfdh_path = data_path(NFDH_FILE)
    if not nfdh_path.exists():
        return []
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    delta = radius_m / 111_000 * 1.5
    sql = f"""
        WITH raw AS (
            SELECT station_id, road_name, lon, lat as slat,
                   aadt, heavy_vehicle_pct, direction
            FROM read_parquet('{nfdh_path}')
            WHERE lon BETWEEN {lng - delta} AND {lng + delta}
              AND lat BETWEEN {lat - delta} AND {lat + delta}
        ),
        agg AS (
            SELECT station_id, road_name, lon, slat,
                   COALESCE(
                       MAX(CASE WHEN direction IS NULL THEN aadt END),
                       SUM(CASE WHEN direction IS NOT NULL THEN aadt END)
                   ) AS total_aadt,
                   MAX(heavy_vehicle_pct) AS hv_pct
            FROM raw
            GROUP BY station_id, road_name, lon, slat
        )
        SELECT total_aadt, hv_pct, road_name,
               SQRT(POW((lon - {lng}) * {m_per_deg}, 2) +
                    POW((slat - {lat}) * 111320, 2)) AS dist_m,
               lon, slat
        FROM agg
        WHERE total_aadt IS NOT NULL
          AND SQRT(POW((lon - {lng}) * {m_per_deg}, 2) +
                   POW((slat - {lat}) * 111320, 2)) < {radius_m}
        ORDER BY dist_m
    """
    return db.sql(sql).fetchall()


def gtfs_rail_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
                   radius_m: int = 1000) -> list[tuple]:
    """Find rail/tram routes near a point using GTFS shapes + frequencies.

    Checks national AU file first, falls back to PTV-only file.
    Returns (route_type, route_name, dist_m, peak_svc_per_hr, offpeak_svc_per_hr).
    route_type: 0=tram, 1=metro, 2=train.
    """
    au_shapes = data_path(AU_RAIL_SHAPES_FILE)
    au_freq = data_path(AU_RAIL_FREQ_FILE)
    if au_shapes.exists() and au_freq.exists():
        shapes_path, freq_path = au_shapes, au_freq
    else:
        shapes_path = data_path(PTV_SHAPES_FILE)
        freq_path = data_path(PTV_FREQ_FILE)
        if not shapes_path.exists() or not freq_path.exists():
            return []

    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    delta = radius_m / 111_000 * 1.5

    sql = f"""
        WITH nearby_shapes AS (
            SELECT shape_id, route_type,
                   MIN(SQRT(POW((lng - {lng}) * {m_per_deg}, 2) +
                            POW((lat - {lat}) * 111320, 2))) AS dist_m
            FROM read_parquet('{shapes_path}')
            WHERE lng BETWEEN {lng - delta} AND {lng + delta}
              AND lat BETWEEN {lat - delta} AND {lat + delta}
            GROUP BY shape_id, route_type
            HAVING dist_m < {radius_m}
        )
        SELECT ns.route_type, f.route_name, ns.dist_m,
               f.peak_services_per_hour, f.offpeak_services_per_hour
        FROM nearby_shapes ns
        JOIN read_parquet('{freq_path}') f
          ON ns.shape_id = f.shape_id
        ORDER BY ns.dist_m
    """
    return db.sql(sql).fetchall()


# Backward-compatible alias
ptv_rail_near = gtfs_rail_near


def pois_near(db: duckdb.DuckDBPyConnection, lat: float, lng: float,
              radius_m: int = 1500, *, source: str | None = None) -> list[tuple]:
    table = f"read_parquet('{source or _local_or_fail(POIS_FILE)}')"
    delta = radius_m / 111_000 * 1.5
    import math
    m_per_deg = 111_320 * math.cos(math.radians(lat))
    deg_thresh = radius_m / m_per_deg
    sql = f"""
        SELECT categories.primary AS category,
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m
        FROM {table}
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
    """
    return db.sql(sql).fetchall()
