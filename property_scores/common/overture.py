"""Overture Maps data loading helpers via DuckDB."""

import duckdb

from property_scores.common.config import data_path

import threading

ROADS_FILE = "overture_roads.parquet"
POIS_FILE = "overture_pois.parquet"

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
               ST_Distance(geometry, ST_Point({lng}, {lat})) * {m_per_deg} AS dist_m
        FROM {table}
        WHERE bbox.xmin BETWEEN {lng - delta} AND {lng + delta}
          AND bbox.ymin BETWEEN {lat - delta} AND {lat + delta}
          AND ST_Distance(geometry, ST_Point({lng}, {lat})) < {deg_thresh}
          AND subtype = 'road'
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
