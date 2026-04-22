"""Overture Maps data loading helpers via DuckDB."""

import duckdb

from property_scores.common.config import data_path

import threading

ROADS_FILE = "overture_roads.parquet"
POIS_FILE = "overture_pois.parquet"
AADT_FILE = "vicroads_aadt_2019.parquet"

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
